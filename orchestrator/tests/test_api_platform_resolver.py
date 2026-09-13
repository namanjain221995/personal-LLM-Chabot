"""Identity resolution and the management layer — CONTRACT-3 §4, §5, §6.

Two modules, one subject: who a `/v1` caller is, and how the console creates
the credential that says so. `resolver.py` turns a Bearer token into an
`ApiCaller`; `projects.py` is what the console calls to mint, rotate and
revoke one. They are tested together because almost every assertion below
needs both — you cannot prove that a revoked key is refused without a key to
revoke.

Real PostgreSQL, no mocks. A tenancy predicate that is only tested against a
fake is not tested at all (the same argument `tests/test_api_platform_db.py`
makes about V34).

FOUR PROPERTIES, and the tests are grouped by them:

1. ONE REFUSAL, MANY REASONS. Unknown, malformed, wrong-secret, revoked,
   expired, rotated-out, disabled service account, disabled project, disabled
   workspace and outside-the-IP-allowlist all produce the SAME `401
   invalid_api_key`, byte for byte in the envelope. `test_every_refusal_…`
   asserts that over every one of them at once rather than one at a time,
   because the property is about the SET.

2. THE TENANT IS THE DATABASE'S DECISION. A key of workspace A cannot reach a
   project of workspace B, and a cross-workspace row reads as missing rather
   than forbidden — a 403 there would be an existence oracle.

3. NARROWING ONLY EVER SHRINKS. A key may hold fewer scopes, fewer models and
   lower limits than its project; it may never hold more, whichever direction
   the numbers point.

4. THE PLAINTEXT EXISTS ONCE AND NO ROW CARRIES A DIGEST OUT. Every public
   view is an allow-list, and the assertion is made over the WHOLE view rather
   than over the one column we remember to look at.
"""
from __future__ import annotations

import inspect
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.apiplatform import keys, projects, resolver
from app.apiplatform.scopes import Scope
from app.config import settings
from app.publicapi import errors

WORKSPACE_A = "ws-resolver-a"
WORKSPACE_B = "ws-resolver-b"

#: 32 characters, `keys.MIN_PEPPER_CHARS` exactly.
TEST_PEPPER = "resolver-pepper-0123456789abcdef"


@pytest.fixture(autouse=True)
def _pepper(monkeypatch):
    """A configured pepper, and no cached one from a neighbouring test.

    `raising=False` is absent on purpose: if `settings.api_key_pepper` ever
    stops existing again, this fixture fails the whole file rather than
    quietly creating the attribute it is asserting on (2026-09-13).
    """
    monkeypatch.setattr(settings, "api_key_pepper", TEST_PEPPER)
    keys.reset_pepper_cache()
    yield
    keys.reset_pepper_cache()


@pytest.fixture(autouse=True)
def _no_workspace_reader():
    """The workspace-status seam is process state; leave it unwired."""
    resolver.configure_workspace_status(None)
    yield
    resolver.configure_workspace_status(None)


@pytest.fixture()
def tenants():
    with db.connection() as con:
        for workspace_id, name in ((WORKSPACE_A, "Alpha"), (WORKSPACE_B, "Beta")):
            con.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, %s)",
                (workspace_id, name),
            )
    return {
        "a": WORKSPACE_A,
        "b": WORKSPACE_B,
        "owner": int(db.create_user("resolver-owner", "hash")),
    }


@pytest.fixture()
def project(tenants):
    return projects.create_project(
        tenants["a"], "Billing integration", created_by=tenants["owner"]
    )


@pytest.fixture()
def created(project, tenants):
    return projects.create_key(
        project["id"], tenants["a"], "server", created_by=tenants["owner"]
    )


def header(token: str) -> str:
    return f"Bearer {token}"


def refusal(authorization, *, ip=None) -> errors.ApiError:
    """Resolve, expect an `ApiError`, hand it back for comparison."""
    with pytest.raises(errors.ApiError) as caught:
        resolver.resolve_api_caller(authorization, ip=ip)
    return caught.value


def envelope(error: errors.ApiError) -> dict:
    return error.envelope("req_fixed")


# ---------------------------------------------------------------------------
# 1. One refusal, many reasons
# ---------------------------------------------------------------------------


def test_every_refusal_is_the_same_401_with_the_same_envelope(project, tenants, created):
    """The single most important property in the resolver, asserted over the
    whole set at once.

    A surface that distinguishes "revoked" from "unknown" tells whoever holds
    a leaked key that it is real and was noticed; one that distinguishes
    "project disabled" from "expired" tells them the tenant exists.
    STANDARDS.md calls the general shape the existence oracle and warns that a
    differing BODY, a differing HEADER SET or a measurably different latency
    all reopen it — so this compares the rendered envelope and the header dict,
    not just the status code.
    """
    workspace = tenants["a"]
    good = created.token

    # (a) a key that never existed
    unknown = keys.mint_key("live").token
    # (b) A GENUINE FORGERY, not a typo: this key's real public id, a
    # different secret, and a checksum recomputed over the pair so the offline
    # layer waves it straight through. The checksum is not a security control
    # — an attacker computes it as easily as we do — so this is the token that
    # reaches `verify_secret` and is refused by the HMAC and nothing else.
    # Flipping a character of the secret instead would break the checksum and
    # would only ever have tested `split_key` again.
    real = keys.split_key(good)
    stranger_secret = keys.mint_key("live").secret
    forged = (
        f"tsk_live_{real.public_id}_{stranger_secret}"
        + keys.checksum_for(real.public_id, stranger_secret)
    )
    assert keys.split_key(forged) is not None  # the offline layer is satisfied
    # (c) a live token presented as a test token
    swapped = "tsk_test_" + good[len("tsk_live_"):]

    # (d) revoked
    revoked_key = projects.create_key(project["id"], workspace, "to-revoke")
    projects.revoke_key(revoked_key.key["id"], workspace)

    # (e) expired
    expired_key = projects.create_key(
        project["id"],
        workspace,
        "already-expired",
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    # (f) its service account is disabled
    account = projects.create_service_account(project["id"], workspace, "batch")
    account_key = projects.create_key(
        project["id"], workspace, "via-account", service_account_id=account["id"]
    )
    with db.connection() as con:
        con.execute(
            "UPDATE api_service_accounts SET status = 'disabled' WHERE id = %s",
            (account["id"],),
        )

    # (g) its project is disabled
    other_project = projects.create_project(workspace, "Disabled project")
    project_key = projects.create_key(other_project["id"], workspace, "in-disabled")
    projects.disable_project(other_project["id"], workspace)

    refusals = {
        "malformed": refusal("Bearer not-a-key"),
        "missing header": refusal(None),
        "wrong scheme": refusal(f"Basic {good}"),
        "a cookie": refusal("ts_session=abc123"),
        "unknown key": refusal(header(unknown)),
        "forged secret": refusal(header(forged)),
        "prefix swapped": refusal(header(swapped)),
        "revoked": refusal(header(revoked_key.token)),
        "expired": refusal(header(expired_key.token)),
        "disabled service account": refusal(header(account_key.token)),
        "disabled project": refusal(header(project_key.token)),
    }

    # (h) the workspace itself switched off — the one rung V34 has no column
    # for, so the resolver's seam is what is exercised.
    resolver.configure_workspace_status(lambda ws: "disabled")
    refusals["disabled workspace"] = refusal(header(good))
    resolver.configure_workspace_status(None)

    # (i) a valid key from outside the project's ip_allowlist
    projects.update_project(project["id"], workspace, ip_allowlist=["203.0.113.0/24"])
    refusals["outside the ip allowlist"] = refusal(header(good), ip="198.51.100.7")
    refusals["unknown source address"] = refusal(header(good), ip=None)

    for reason, error in refusals.items():
        assert error.status == 401, reason
        assert error.code == "invalid_api_key", reason
        assert error.type == "authentication_error", reason
        assert envelope(error) == envelope(errors.invalid_api_key()), reason
        assert error.headers() == {}, reason
        # No quota state on a 401 (draft-ietf-httpapi-ratelimit-headers-11's
        # Security Considerations: it would let a stranger infer a tenant's
        # traffic), and nothing that names the reason.
        assert "RateLimit" not in error.headers(), reason

    # And the set is not accidentally small: each of those is a distinct rung.
    assert len(refusals) == 14


def test_a_rotated_out_key_stops_working_when_its_grace_window_shuts(project, tenants):
    """`rotation_expires_at` is an independent deadline from `expires_at`: a
    key can be inside its own lifetime and still be a credential that was
    replaced last week."""
    workspace = tenants["a"]
    created = projects.create_key(project["id"], workspace, "rotating")
    caller = resolver.resolve_api_caller(header(created.token))
    assert caller.key_id == created.key["id"]

    with db.connection() as con:
        con.execute(
            "UPDATE api_keys SET rotation_expires_at = now() - interval '1 second' "
            "WHERE id = %s",
            (created.key["id"],),
        )

    assert refusal(header(created.token)).code == "invalid_api_key"


def test_an_unknown_public_id_still_pays_for_an_hmac(project, tenants, created, monkeypatch):
    """The timing half of "one refusal, many reasons".

    An unknown `public_id` never reaches `verify_secret`, so without a
    deliberate dummy comparison the "no such key" path is measurably cheaper
    than "wrong secret on a real key" and a patient attacker learns which
    public ids exist. This asserts the CALL rather than a wall-clock
    measurement, because a timing assertion in CI is a flake generator.
    """
    calls = []
    real = keys.verify_secret
    monkeypatch.setattr(
        keys, "verify_secret", lambda s, d: calls.append(d) or real(s, d)
    )

    refusal(header(keys.mint_key("live").token))

    assert calls == [resolver._DUMMY_DIGEST]


def test_the_resolver_has_no_way_to_receive_a_cookie(created):
    """CONTRACT-3 §1: `/v1` reads exactly one credential and ignores `Cookie`
    entirely. Asserted structurally — the function's signature is the
    guarantee, not a branch somebody could delete."""
    signature = inspect.signature(resolver.resolve_api_caller)

    # `peer` / `forwarded_for` are the two STRINGS the trusted-proxy rule
    # needs (2026-09-13); the request object itself is still never accepted.
    assert set(signature.parameters) == {
        "authorization_header", "ip", "authorization", "peer", "forwarded_for",
    }
    assert not any("cookie" in name.lower() for name in signature.parameters)
    assert not any("request" in name.lower() for name in signature.parameters)
    # And a session cookie offered in the header it DOES read is just a bad
    # token, refused like any other.
    assert refusal(f"Bearer ts_session={created.token}").code == "invalid_api_key"


def test_the_router_can_find_this_signature_by_the_name_it_inspects_for(created):
    """`app/publicapi/router.py::_call_resolver` was written before this
    function existed and adapts by INSPECTING the signature: it looks for a
    parameter named `request` or `authorization` and, finding neither, calls
    `fn(request)` positionally — which would hand a FastAPI `Request` object
    to a function expecting a string. Every direct-call test would still pass
    and `/v1` would answer 401 to every request in production.

    So `authorization` is accepted as a keyword alias, and this pins it by
    performing the router's own dispatch rather than by describing it.
    """
    parameters = inspect.signature(resolver.resolve_api_caller).parameters
    assert "request" not in parameters
    assert "authorization" in parameters  # the branch the router will take

    caller = resolver.resolve_api_caller(authorization=header(created.token))
    assert caller.key_id == created.key["id"]
    # And the two spellings are the same header, not two different inputs.
    assert (
        resolver.resolve_api_caller(header(created.token)).key_id
        == caller.key_id
    )


def test_a_prefix_swapped_token_is_refused_with_the_same_401_as_any_other(
    project, tenants, created
):
    """The environment is outside CONTRACT-3 §5's checksum, so `split_key`
    accepts a token whose `live`/`test` label was swapped and reports the
    attacker's choice. The reconciliation against `api_keys.environment` is
    what makes that harmless, and it lives here because the parser cannot know
    what was stored."""
    swapped = "tsk_test_" + created.token[len("tsk_live_"):]

    assert keys.split_key(swapped) is not None  # the parser is happy
    assert keys.split_key(swapped).environment == "test"
    assert refusal(header(swapped)).code == "invalid_api_key"
    # The honest token still works, so the refusal is about the label and not
    # about having broken the key.
    assert resolver.resolve_api_caller(header(created.token)).environment == "live"


# ---------------------------------------------------------------------------
# 2. The tenant is the database's decision
# ---------------------------------------------------------------------------


def test_a_key_of_workspace_a_cannot_address_a_project_of_workspace_b(tenants):
    """The cross-tenant question, asked from both ends.

    `db.create_api_key` refuses to create a key in another workspace's project
    at all, and every management read is scoped by workspace — so a key of A
    never becomes a credential for B's project, and B's project is INVISIBLE
    to A rather than forbidden to it.
    """
    project_a = projects.create_project(tenants["a"], "A's project")
    project_b = projects.create_project(tenants["b"], "B's project")

    # A cannot mint into B's project.
    with pytest.raises(ValueError):
        projects.create_key(project_b["id"], tenants["a"], "stolen")

    # A cannot read B's project, and it reads as MISSING rather than refused.
    assert projects.get_project(project_b["id"], tenants["a"]) is None
    assert projects.list_projects(tenants["a"]) == [
        projects.get_project(project_a["id"], tenants["a"])
    ]

    # A's key resolves to A's workspace and A's project, and nothing else.
    created = projects.create_key(project_a["id"], tenants["a"], "server")
    caller = resolver.resolve_api_caller(header(created.token))
    assert caller.workspace_id == tenants["a"]
    assert caller.project_id == project_a["id"]

    # B's key cannot be updated or revoked through A's workspace.
    created_b = projects.create_key(project_b["id"], tenants["b"], "theirs")
    assert projects.revoke_key(created_b.key["id"], tenants["a"]) is None
    assert projects.get_key(created_b.key["id"], project_b["id"], tenants["a"]) is None
    assert resolver.resolve_api_caller(header(created_b.token)).workspace_id == tenants["b"]


def test_a_key_whose_row_disagrees_with_its_project_about_the_tenant_is_refused(
    project, tenants, created
):
    """Every V34 table stores `workspace_id` explicitly so a quota query never
    has to guess — which means the two copies CAN disagree, and a corrupted
    tenant boundary must be a refusal rather than a coin toss about which
    copy wins."""
    with db.connection() as con:
        con.execute(
            "UPDATE api_keys SET workspace_id = %s WHERE id = %s",
            (tenants["b"], created.key["id"]),
        )

    assert refusal(header(created.token)).code == "invalid_api_key"


# ---------------------------------------------------------------------------
# 3. Narrowing only ever shrinks
# ---------------------------------------------------------------------------


def test_a_key_may_lower_its_projects_limits_and_never_raise_them(project, tenants):
    """`api_keys.rpm` and `max_concurrency` are on a row the console can edit.
    A field on an editable row that RAISES a limit is a privilege-escalation
    field, so a higher number is clamped down rather than honoured."""
    workspace = tenants["a"]
    projects.update_project(project["id"], workspace, rpm=60, max_concurrency=4)

    inherits = projects.create_key(project["id"], workspace, "inherits")
    narrower = projects.create_key(project["id"], workspace, "narrow", rpm=10, max_concurrency=1)
    greedy = projects.create_key(project["id"], workspace, "greedy", rpm=6000, max_concurrency=99)

    assert resolver.resolve_api_caller(header(inherits.token)).limits.rpm == 60
    assert resolver.resolve_api_caller(header(narrower.token)).limits.rpm == 10
    assert resolver.resolve_api_caller(header(narrower.token)).limits.max_concurrency == 1
    assert resolver.resolve_api_caller(header(greedy.token)).limits.rpm == 60
    assert resolver.resolve_api_caller(header(greedy.token)).limits.max_concurrency == 4


def test_a_service_account_caps_the_scopes_of_the_keys_under_it(project, tenants):
    """The account is a ceiling, not a grant: a key holds the intersection.

    And the two empties mean different things on purpose — an empty list on
    the KEY grants nothing (a credential denies by default), while an empty
    list on the ACCOUNT narrows nothing (an account is an organisational
    grouping whose DDL default is `'[]'`, and reading that as "permits
    nothing" would make every key attached to one useless).
    """
    workspace = tenants["a"]
    account = projects.create_service_account(
        project["id"], workspace, "read-only", scopes=[Scope.MODELS_READ]
    )
    broad = projects.create_key(
        project["id"],
        workspace,
        "wants-everything",
        service_account_id=account["id"],
        scopes=[Scope.MODELS_READ, Scope.RESPONSES_WRITE, Scope.USAGE_READ],
    )
    assert resolver.resolve_api_caller(header(broad.token)).scopes == {Scope.MODELS_READ}

    parked = projects.create_key(project["id"], workspace, "parked", scopes=[])
    assert resolver.resolve_api_caller(header(parked.token)).scopes == frozenset()

    with db.connection() as con:
        con.execute(
            "UPDATE api_service_accounts SET scopes = '[]'::jsonb WHERE id = %s",
            (account["id"],),
        )
    assert resolver.resolve_api_caller(header(broad.token)).scopes == {
        Scope.MODELS_READ,
        Scope.RESPONSES_WRITE,
        Scope.USAGE_READ,
    }


def test_a_scope_this_release_does_not_know_refuses_the_whole_credential(
    project, tenants, created
):
    """Dropping an unrecognised entry would produce a key that looks configured
    and grants less than its row says. `scopes.parse_scopes` raises for exactly
    that reason, and the resolver turns it into the generic 401."""
    with db.connection() as con:
        con.execute(
            """UPDATE api_keys SET scopes = '["models.read","webhooks.manage"]'::jsonb
               WHERE id = %s""",
            (created.key["id"],),
        )

    assert refusal(header(created.token)).code == "invalid_api_key"


def test_the_model_allowlist_narrows_project_then_account_then_key(project, tenants):
    """Three levels, each able only to shrink. An empty list at a level means
    that level expressed no opinion — `allowed_models` defaults to `'[]'` and
    every project would otherwise be born unable to call anything."""
    workspace = tenants["a"]
    assert resolver.resolve_api_caller(
        header(projects.create_key(project["id"], workspace, "open").token)
    ).models == ()

    projects.update_project(
        project["id"], workspace, allowed_models=["techsara-35b", "techsara-mini"]
    )
    narrowed = projects.create_key(
        project["id"], workspace, "one-model", allowed_models=["techsara-35b"]
    )
    assert resolver.resolve_api_caller(header(narrowed.token)).models == ("techsara-35b",)

    # A key naming a model its project does not allow is narrowed to nothing,
    # NOT widened — and "nothing" must not be confused with "no narrowing".
    off_menu = projects.create_key(
        project["id"], workspace, "off-menu", allowed_models=["some-other-model"]
    )
    resolved = resolver.resolve_api_caller(header(off_menu.token)).models
    assert resolved == (resolver._NO_MODEL,)
    assert "techsara-35b" not in resolved


# ---------------------------------------------------------------------------
# The IP allowlist
# ---------------------------------------------------------------------------


def test_an_empty_ip_allowlist_permits_everything_and_a_populated_one_is_closed(
    project, tenants, created
):
    """Opt-in, and closed once opted into. STANDARDS.md (Stripe, Cloudflare)
    recommends it on every live key: it turns a stolen key into one that only
    works from the customer's own infrastructure."""
    workspace = tenants["a"]
    assert resolver.resolve_api_caller(header(created.token), ip="198.51.100.7")

    projects.update_project(
        project["id"], workspace, ip_allowlist=["203.0.113.9", "2001:db8::/32"]
    )
    assert resolver.resolve_api_caller(header(created.token), ip="203.0.113.9")
    assert resolver.resolve_api_caller(header(created.token), ip="2001:db8::1")
    assert refusal(header(created.token), ip="203.0.113.10").code == "invalid_api_key"
    # "We could not tell" is not one of the addresses the operator listed.
    assert refusal(header(created.token), ip=None).code == "invalid_api_key"


@pytest.mark.parametrize(
    "ip,allowlist,expected",
    [
        ("10.0.0.5", [], True),
        (None, [], True),
        ("10.0.0.5", ["10.0.0.0/8"], True),
        ("11.0.0.5", ["10.0.0.0/8"], False),
        ("10.0.0.5", ["not-an-address"], False),
        ("10.0.0.5", ["not-an-address", "10.0.0.0/8"], True),
        ("not-an-address", ["10.0.0.0/8"], False),
        ("::1", ["127.0.0.1"], False),
        ("2001:db8::1", ["2001:db8::/32"], True),
    ],
)
def test_the_ip_allowlist_matches_addresses_and_cidrs_and_skips_a_typo(
    ip, allowlist, expected
):
    """A malformed entry is SKIPPED rather than treated as a match: a typo in
    the console must not silently open the list."""
    assert resolver.ip_allowed(ip, allowlist) is expected


# ---------------------------------------------------------------------------
# The successful path
# ---------------------------------------------------------------------------


def test_a_good_key_resolves_to_the_identity_contract_section_four_names(
    project, tenants, created
):
    caller = resolver.resolve_api_caller(header(created.token), ip="203.0.113.9")

    assert caller.workspace_id == tenants["a"]
    assert caller.project_id == project["id"]
    assert caller.key_id == created.key["id"]
    assert caller.service_account_id is None
    assert caller.environment == "live"
    assert caller.public_id == created.key["public_id"]
    assert caller.scopes == {
        Scope.MODELS_READ,
        Scope.RESPONSES_READ,
        Scope.RESPONSES_WRITE,
    }
    assert caller.limits.rpm == 60 and caller.limits.daily_token_quota == 2_000_000
    assert caller.ip == "203.0.113.9"
    # The log form is built from the STORED environment, never the presented
    # one, and carries no part of the secret.
    assert caller.redacted_key() == f"tsk_live_{caller.public_id}_<redacted>"
    assert created.token not in caller.redacted_key()


def test_the_key_is_touched_exactly_once_per_request_and_never_per_token(
    project, tenants, created, monkeypatch
):
    """`last_used_at` answers "is this integration still alive" and
    `last_used_ip` answers "where is this leaked key being used from". Neither
    needs more resolution than one row per request, and writing per token
    would mean 800 write transactions for one generation."""
    calls = []
    real = db.touch_api_key
    monkeypatch.setattr(
        db, "touch_api_key", lambda key_id, ip=None: calls.append((key_id, ip)) or real(key_id, ip)
    )

    resolver.resolve_api_caller(header(created.token), ip="203.0.113.9")

    assert calls == [(created.key["id"], "203.0.113.9")]
    stored = projects.get_key(created.key["id"], project["id"], tenants["a"])
    assert stored["last_used_ip"] == "203.0.113.9"
    assert stored["last_used_at"] is not None


def test_a_refused_key_is_never_touched(project, tenants, created, monkeypatch):
    """`last_used_at` must mean USED. A revoked key that keeps updating its own
    timestamp makes the "watch it stop moving, then revoke" rotation step —
    Stripe's, and the one `keys.DEFAULT_ROTATION_OVERLAP` is sized for —
    report a credential that nothing is using as live."""
    monkeypatch.setattr(
        db,
        "touch_api_key",
        lambda *a, **k: pytest.fail("a refused key must not be touched"),
    )
    projects.revoke_key(created.key["id"], tenants["a"])

    assert refusal(header(created.token)).code == "invalid_api_key"


def test_a_revocation_takes_effect_on_the_very_next_request(project, tenants, created):
    """There is no cached key state anywhere (CONTRACT-3 §5), so a revocation
    is a single UPDATE with no TTL to wait out. STANDARDS.md names a cached
    key state as the failure that makes revocation silently ineffective."""
    assert resolver.resolve_api_caller(header(created.token)).key_id == created.key["id"]

    projects.revoke_key(created.key["id"], tenants["a"], revoked_by=tenants["owner"])

    assert refusal(header(created.token)).code == "invalid_api_key"


# ---------------------------------------------------------------------------
# 4. The plaintext exists once; no view carries a digest out
# ---------------------------------------------------------------------------


def test_the_plaintext_key_is_returned_exactly_once_and_stored_nowhere(
    project, tenants, created
):
    token = created.token

    # Not in the row that was returned, not in any later read, not in the
    # database at all.
    assert token not in repr(created.key)
    assert "key_hash" not in created.key
    for row in projects.list_keys(project["id"], tenants["a"]):
        assert "key_hash" not in row
        assert token not in repr(row)
    with db.connection() as con:
        stored = con.execute(
            "SELECT key_hash, last_four FROM api_keys WHERE id = %s",
            (created.key["id"],),
        ).fetchone()
    assert stored["key_hash"] == keys.key_digest(keys.split_key(token).secret)
    # Every column of the row, rendered, carries neither the token nor the
    # secret. (The old assertion — the secret is not a substring of the hex
    # digest — could never fail: a urlsafe secret is not lowercase hex.)
    secret = keys.split_key(token).secret
    with db.connection() as con:
        whole = con.execute(
            "SELECT * FROM api_keys WHERE id = %s", (created.key["id"],)
        ).fetchone()
    for column, value in whole.items():
        assert token not in str(value), column
        assert secret not in str(value), column


def test_a_created_key_never_renders_its_token(created):
    """A repr lands in a traceback, a debugger, pytest's own failure output
    and the f-string somebody adds to a log line at 2am."""
    for rendered in (repr(created), str(created), f"{created}", format(created)):
        assert created.token not in rendered
        assert "<redacted>" in rendered
        assert created.key["public_id"] in rendered


def test_no_public_view_can_ever_carry_secret_material():
    """Asserted over the ALLOW-LISTS themselves, not over one sample row: the
    property must hold for every future column too. OWASP API3:2023 —
    allow-list the response properties per endpoint, because a deny-list
    starts leaking the day somebody adds `key_hash_v2`."""
    for fields in (
        projects._KEY_FIELDS,
        projects._PROJECT_FIELDS,
        projects._SERVICE_ACCOUNT_FIELDS,
    ):
        assert not (projects.SECRET_COLUMNS & set(fields))
        assert not any("hash" in name or "secret" in name for name in fields)

    # And a row that arrives WITH the forbidden columns loses them.
    view = projects.public_key_view(
        {"id": "key_1", "public_id": "abc", "key_hash": "DIGEST", "secret": "SHHH"}
    )
    assert view == {"id": "key_1", "public_id": "abc"}


# ---------------------------------------------------------------------------
# Rotation and revocation are different operations
# ---------------------------------------------------------------------------


def test_rotating_with_no_overlap_kills_the_old_key_in_the_same_call(project, tenants):
    """The compromise path. STANDARDS.md: a single "rotate" verb with a
    built-in grace period cannot serve a live compromise, where the whole
    point is that the old credential stops working now."""
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "leaked")

    rotated = projects.revoke_key_now(old.key["id"], project["id"], workspace)

    assert rotated.previous_revoked is True
    assert rotated.previous_public_id == old.key["public_id"]
    assert refusal(header(old.token)).code == "invalid_api_key"
    assert resolver.resolve_api_caller(header(rotated.token)).key_id == rotated.created.key["id"]
    assert rotated.created.key["rotated_from"] == old.key["public_id"]
    # The REPLACEMENT carries no grace window of its own. `rotation_expires_at`
    # means "when does THIS key stop working"; writing the predecessor's
    # deadline onto the replacement would kill the new key at birth, because a
    # zero-overlap rotation sets that deadline to now.
    assert rotated.created.key["rotation_expires_at"] is None


def test_a_planned_rotation_leaves_the_old_key_working_and_says_until_when(
    project, tenants
):
    """The planned path: the old key keeps working through the overlap, and
    the deadline is WRITTEN onto the old row (2026-09-13) rather than only
    returned for somebody to remember."""
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "rolling")

    rotated = projects.rotate_key(old.key["id"], project["id"], workspace)

    assert rotated.previous_revoked is False
    assert rotated.previous_expires_at > datetime.now(timezone.utc) + timedelta(days=6)
    stored = projects.get_key(old.key["id"], project["id"], workspace)
    assert datetime.fromisoformat(stored["rotation_expires_at"]) == rotated.previous_expires_at
    assert resolver.resolve_api_caller(header(old.token)).key_id == old.key["id"]
    assert resolver.resolve_api_caller(header(rotated.token)).key_id != old.key["id"]
    # The replacement inherits what the old key could do; a rotation is not a
    # quiet change of permissions.
    assert rotated.created.key["scopes"] == old.key["scopes"]


def test_a_rotation_never_renders_the_new_token(project, tenants):
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "rolling")
    rotated = projects.rotate_key(old.key["id"], project["id"], workspace)

    for rendered in (repr(rotated), str(rotated), f"{rotated}"):
        assert rotated.token not in rendered
        assert "<redacted>" in rendered


def test_a_new_key_gets_a_finite_life_unless_somebody_says_otherwise(project, tenants):
    """STANDARDS.md (OWASP Secrets Management): rotation bounds the useful life
    of a stolen key only if the key has a life to bound. Never-expiring is
    available, and it has to be asked for."""
    workspace = tenants["a"]
    default = projects.create_key(project["id"], workspace, "ninety-days")
    forever = projects.create_key(project["id"], workspace, "forever", expires_at=False)

    expiry = datetime.fromisoformat(default.key["expires_at"])
    assert expiry > datetime.now(timezone.utc) + timedelta(days=89)
    assert forever.key["expires_at"] is None


# ---------------------------------------------------------------------------
# The workspace rung V34 has no column for
# ---------------------------------------------------------------------------


def test_an_unwired_workspace_reader_reports_active_and_a_broken_one_refuses():
    """The seam is documented as failing OPEN when nothing is wired — there is
    no way to disable a workspace in V34, so "active" is the only answer the
    data can give — and failing CLOSED when a wired reader misbehaves, because
    a workspace whose status cannot be determined is not one to serve API
    traffic for."""
    assert resolver.workspace_status("anything") == "active"

    def broken(workspace_id):
        raise RuntimeError("the status service is down")

    resolver.configure_workspace_status(broken)
    assert resolver.workspace_status("anything") != "active"

    resolver.configure_workspace_status(lambda ws: "  ACTIVE  ")
    assert resolver.workspace_status("anything") == "active"



# ---------------------------------------------------------------------------
# 5. The wave-2 review, 2026-09-13
# ---------------------------------------------------------------------------


def test_a_rotated_out_key_is_refused_by_the_resolver_once_its_overlap_has_passed(
    project, tenants
):
    """No hand-written UPDATE this time: `rotate_key` writes the deadline on
    the OLD row, and the resolver refuses that key from that instant. The
    rotation is performed as of two hours ago with a one-hour overlap, so the
    deadline is already an hour in the past."""
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "rolling")
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)

    rotated = projects.rotate_key(
        old.key["id"], project["id"], workspace,
        overlap=timedelta(hours=1), now=two_hours_ago,
    )

    assert refusal(header(old.token)).code == "invalid_api_key"
    # The replacement is alive and carries no deadline of its own.
    assert resolver.resolve_api_caller(header(rotated.token)).key_id == rotated.created.key["id"]
    assert rotated.created.key["rotation_expires_at"] is None
    assert rotated.created.key["rotated_from"] == old.key["public_id"]


def test_a_planned_rotation_uses_the_configured_overlap(project, tenants, monkeypatch):
    monkeypatch.setattr(settings, "public_api_key_rotation_overlap_hours", 2.0)
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "rolling")
    moment = datetime.now(timezone.utc)

    rotated = projects.rotate_key(old.key["id"], project["id"], workspace, now=moment)

    assert rotated.previous_expires_at == moment + timedelta(hours=2)
    assert resolver.resolve_api_caller(header(old.token)).key_id == old.key["id"]


def test_the_new_plaintext_key_is_returned_once_and_never_by_a_later_read(project, tenants):
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "rolling")
    rotated = projects.rotate_key(old.key["id"], project["id"], workspace)
    secret = keys.split_key(rotated.token).secret

    for row in projects.list_keys(project["id"], workspace):
        assert rotated.token not in repr(row)
        assert secret not in repr(row)
    with db.connection() as con:
        for row in con.execute("SELECT * FROM api_keys").fetchall():
            assert all(secret not in str(value) for value in row.values())


def test_two_simultaneous_rotations_of_one_key_mint_exactly_one_replacement(
    project, tenants
):
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "contested")
    gate = threading.Barrier(6)
    outcomes, lock = [], threading.Lock()

    def rotate():
        gate.wait(timeout=30)
        try:
            projects.rotate_key(old.key["id"], project["id"], workspace)
            result = "rotated"
        except ValueError:
            result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=rotate) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(outcomes) == ["refused"] * 5 + ["rotated"]
    successors = [
        row for row in projects.list_keys(project["id"], workspace)
        if row["rotated_from"] == old.key["public_id"]
    ]
    assert len(successors) == 1


def test_a_key_inside_a_rotation_can_still_be_killed_at_once_when_it_leaks(project, tenants):
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "rolling")
    projects.rotate_key(old.key["id"], project["id"], workspace)
    with pytest.raises(ValueError):
        projects.rotate_key(old.key["id"], project["id"], workspace)

    killed = projects.revoke_key_now(old.key["id"], project["id"], workspace)

    assert killed.previous_revoked is True
    assert refusal(header(old.token)).code == "invalid_api_key"


def test_a_failed_mint_takes_the_rotation_deadline_back_off_the_old_key(
    project, tenants, monkeypatch
):
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "rolling")

    def broken(*args, **kwargs):
        raise RuntimeError("insert failed")

    with monkeypatch.context() as patched:
        patched.setattr(db, "create_api_key", broken)
        with pytest.raises(RuntimeError):
            projects.rotate_key(old.key["id"], project["id"], workspace)

    assert projects.get_key(old.key["id"], project["id"], workspace)["rotation_expires_at"] is None
    assert resolver.resolve_api_caller(header(old.token)).key_id == old.key["id"]


def test_a_rotation_cannot_reach_a_key_of_another_workspace(tenants):
    victim_project = projects.create_project(tenants["b"], "Victim")
    victim = projects.create_key(victim_project["id"], tenants["b"], "prod")
    attacker_project = projects.create_project(tenants["a"], "Attacker")

    with pytest.raises(ValueError):
        projects.rotate_key(victim.key["id"], attacker_project["id"], tenants["a"])
    with pytest.raises(ValueError):
        projects.rotate_key(victim.key["id"], victim_project["id"], tenants["a"])
    assert resolver.resolve_api_caller(header(victim.token)).key_id == victim.key["id"]


@pytest.mark.parametrize(
    "peer,forwarded,trusted,expected",
    [
        # An untrusted peer: the header is the client's own string, ignored.
        ("198.51.100.7", "203.0.113.9", [], "198.51.100.7"),
        ("198.51.100.7", "203.0.113.9", ["10.0.0.0/8"], "198.51.100.7"),
        # A trusted proxy: the rightmost hop that is not one of ours.
        ("10.0.0.2", "203.0.113.9", ["10.0.0.0/8"], "203.0.113.9"),
        # The client prepended a forged allowlisted address; the proxy
        # appended the real one. The real one wins.
        ("10.0.0.2", "203.0.113.9, 198.51.100.7", ["10.0.0.0/8"], "198.51.100.7"),
        ("10.0.0.2", "198.51.100.7, 10.0.0.3", ["10.0.0.0/8"], "198.51.100.7"),
        # A trusted peer and no header: a direct call from the proxy network.
        ("10.0.0.2", None, ["10.0.0.0/8"], "10.0.0.2"),
        # Garbage in the part of the chain we must read: we cannot tell.
        ("10.0.0.2", "not-an-ip", ["10.0.0.0/8"], None),
        # A dual-stack socket's IPv4-mapped peer is the IPv4 address.
        ("::ffff:10.0.0.2", "203.0.113.9", ["10.0.0.0/8"], "203.0.113.9"),
        (None, "203.0.113.9", ["10.0.0.0/8"], None),
    ],
)
def test_a_forwarded_address_is_believed_only_from_a_trusted_proxy(
    peer, forwarded, trusted, expected
):
    assert resolver.client_address(peer, forwarded, trusted_proxies=trusted) == expected


def test_a_forged_forwarded_for_cannot_satisfy_the_ip_allowlist(
    project, tenants, created, monkeypatch
):
    """The LAN forgery: production trusted `X-Forwarded-For` on a global
    boolean with the orchestrator published on 0.0.0.0:8080. A caller that is
    not a configured proxy gets its SOCKET address checked, whatever it
    claims."""
    monkeypatch.setattr(settings, "public_api_trusted_proxies", ("172.18.0.0/16",))
    projects.update_project(project["id"], tenants["a"], ip_allowlist=["203.0.113.9"])

    with pytest.raises(errors.ApiError) as caught:
        resolver.resolve_api_caller(
            header(created.token), peer="192.168.1.50", forwarded_for="203.0.113.9"
        )
    assert caught.value.code == "invalid_api_key"

    via_proxy = resolver.resolve_api_caller(
        header(created.token), peer="172.18.0.4", forwarded_for="203.0.113.9"
    )
    assert via_proxy.ip == "203.0.113.9"

    # With no proxy configured, even the real proxy's header is not believed.
    monkeypatch.setattr(settings, "public_api_trusted_proxies", ())
    with pytest.raises(errors.ApiError):
        resolver.resolve_api_caller(
            header(created.token), peer="172.18.0.4", forwarded_for="203.0.113.9"
        )


def test_a_project_limit_stored_as_zero_is_resolved_as_zero_and_only_null_inherits(
    project, tenants
):
    """The console can write 0 to freeze a project; the first cut enforced
    60 / 2,000,000 / 4 instead."""
    workspace = tenants["a"]
    projects.update_project(
        project["id"], workspace, rpm=0, daily_token_quota=0, max_concurrency=0,
        input_tpm=0, output_tpm=0,
    )
    inherits = projects.create_key(project["id"], workspace, "inherits")
    limits = resolver.resolve_api_caller(header(inherits.token)).limits
    assert (limits.rpm, limits.daily_token_quota, limits.max_concurrency) == (0, 0, 0)
    assert (limits.input_tpm, limits.output_tpm) == (0, 0)
    assert (limits.project_rpm, limits.project_max_concurrency) == (0, 0)
    assert limits.key_rpm is None and limits.key_max_concurrency is None

    # And a NULL column — which only a row that predates the column can hold,
    # since the DDL is NOT NULL — does fall back to the platform default.
    fallback = resolver._effective_limits({}, {"rpm": None, "max_concurrency": None})
    assert fallback.rpm == settings.public_api_default_rpm
    assert fallback.max_concurrency == settings.public_api_default_max_concurrency


def test_a_key_limit_of_zero_parks_the_key_and_null_inherits_the_project(project, tenants):
    workspace = tenants["a"]
    parked = projects.create_key(project["id"], workspace, "parked", rpm=0, max_concurrency=0)
    limits = resolver.resolve_api_caller(header(parked.token)).limits
    assert (limits.rpm, limits.max_concurrency) == (0, 0)
    assert (limits.key_rpm, limits.key_max_concurrency) == (0, 0)
    assert (limits.project_rpm, limits.project_max_concurrency) == (60, 4)


# ---------------------------------------------------------------------------
# projects.rotate_key is the ONE rotation, and it is safe for the shared pool
# (2026-09-13, wave-3 re-verify)
# ---------------------------------------------------------------------------

import contextlib  # noqa: E402


@pytest.fixture()
def one_connection_at_a_time(monkeypatch):
    """Record any checkout of a SECOND pooled connection by a thread that
    still holds one. The console's former rotation held an advisory lock on
    one connection while `db.list_api_keys` / `db.create_api_key` /
    `db.revoke_api_key` each opened another — at pool_max concurrent
    rotations, every holder waits for a connection none of them releases."""
    depth = threading.local()
    violations = []
    real = db.connection

    @contextlib.contextmanager
    def guarded():
        level = getattr(depth, "level", 0)
        if level:
            violations.append(threading.get_ident())
        depth.level = level + 1
        try:
            with real() as con:
                yield con
        finally:
            depth.level = level

    monkeypatch.setattr(db, "connection", guarded)
    return violations


def test_twelve_racing_rotations_mint_one_replacement_without_ever_nesting_pool_checkouts(
    project, tenants, one_connection_at_a_time
):
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "contested")
    gate = threading.Barrier(12)
    outcomes, lock = [], threading.Lock()

    def rotate():
        try:
            gate.wait(timeout=30)
            projects.rotate_key(old.key["id"], project["id"], workspace)
            result = "rotated"
        except projects.RotationRefused as exc:
            result = exc.status
        except Exception as exc:  # noqa: BLE001
            result = repr(exc)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=rotate, daemon=True) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert all(not thread.is_alive() for thread in threads), "a rotation hung"
    assert sorted(outcomes, key=str) == sorted([409] * 11 + ["rotated"], key=str)
    assert one_connection_at_a_time == []
    killed = projects.revoke_key_now(old.key["id"], project["id"], workspace)
    assert killed.previous_revoked is True
    assert one_connection_at_a_time == []


def test_a_rotation_refusal_says_whether_the_key_is_missing_or_merely_not_rotatable(
    project, tenants
):
    """The console maps these to 404 and 409; both stay ValueErrors for the
    callers written against the earlier contract."""
    workspace = tenants["a"]
    with pytest.raises(projects.RotationRefused) as missing:
        projects.rotate_key("key_does_not_exist", project["id"], workspace)
    assert missing.value.status == 404
    assert isinstance(missing.value, ValueError)

    old = projects.create_key(project["id"], workspace, "once")
    rotated = projects.rotate_key(old.key["id"], project["id"], workspace)
    assert rotated.previous is not None
    assert rotated.previous["id"] == old.key["id"]
    assert rotated.previous["rotation_expires_at"] is not None
    assert "key_hash" not in rotated.previous
    with pytest.raises(projects.RotationRefused) as busy:
        projects.rotate_key(old.key["id"], project["id"], workspace)
    assert busy.value.status == 409


def test_a_rotation_with_no_pepper_leaves_the_old_key_untouched_even_on_the_compromise_path(
    project, tenants, monkeypatch
):
    """The digest is computed BEFORE the claim. Computed after it, a
    deployment whose pepper is unavailable revoked the old key (overlap 0)
    and then failed to mint the replacement — a working integration broken
    by a configuration error, with nothing to show for it."""
    workspace = tenants["a"]
    old = projects.create_key(project["id"], workspace, "prod")

    def no_pepper(secret):
        raise keys.PepperUnavailable("no pepper in this test")

    with monkeypatch.context() as patched:
        patched.setattr(keys, "key_digest", no_pepper)
        with pytest.raises(keys.PepperUnavailable):
            projects.revoke_key_now(old.key["id"], project["id"], workspace)
        with pytest.raises(keys.PepperUnavailable):
            projects.rotate_key(old.key["id"], project["id"], workspace)

    row = projects.get_key(old.key["id"], project["id"], workspace)
    assert row["status"] == "active"
    assert row["rotation_expires_at"] is None
