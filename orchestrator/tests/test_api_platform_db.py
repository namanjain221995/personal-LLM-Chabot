"""V34 — the durable state the developer platform stands on.

Everything the public API decides on a request path is decided from these
tables: which key this is, whose project it belongs to, what it has already
spent this minute, whether this idempotency key has been claimed, and where a
completed response should be posted. So the properties pinned here are the
ones whose failure is a security incident rather than a bug report:

  * a row of workspace A is INVISIBLE through workspace B, in the WHERE clause
    and not in an `if` afterwards;
  * a claim is won exactly once, by the database, with no window in the middle;
  * two concurrent usage bumps both land, because a lost update is a quota
    somebody else gets to spend;
  * deleting a tenant takes every child row with it;
  * the model overrides can only narrow what the code declares;
  * a column an authorization decision is read from cannot hold a shape no
    reader can evaluate;
  * no list read hands out secret material — not the key digest, not a
    webhook's signing secret.

Real PostgreSQL, no mocks — a tenancy predicate that is only tested against a
fake is not tested at all.

EVERY TENANCY PREDICATE IS EXERCISED WITH THE WRONG TENANT. The independent
verifier of 2026-09-12 checked this file the way a mutation tester would:
delete `AND workspace_id = %s` from `list_service_accounts` or
`list_webhook_endpoints` and the whole suite stayed green, because both were
only ever called as their owner. A test that never asks the forbidden question
does not pin the answer. So each such function is called twice here — once by
the workspace that owns the row, once by the other one — and the second call
is the assertion that matters.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app import db

WORKSPACE_A = "ws-platform-a"
WORKSPACE_B = "ws-platform-b"

#: Every table V34 creates. Spelled out so the schema sweeps below cannot
#: quietly stop covering one: the first version of this file swept
#: `relname LIKE 'api\_%'`, which excluded `public_models` and
#: `platform_secrets` from the constraint check without saying so, and would
#: have swept in any later migration's `api_`-prefixed table without anybody
#: deciding that.
V34_TABLES = (
    "api_projects", "api_service_accounts", "api_keys", "api_responses",
    "api_idempotency", "api_usage_minute", "api_usage_daily",
    "api_webhook_endpoints", "api_webhook_deliveries", "public_models",
    "platform_secrets",
)


@pytest.fixture()
def tenants():
    """Two workspaces and an owner, which is the smallest world in which
    "another tenant cannot see this" is a question that can be asked."""
    with db.connection() as con:
        for workspace_id, name in ((WORKSPACE_A, "Alpha"), (WORKSPACE_B, "Beta")):
            con.execute(
                "INSERT INTO workspaces (id, name) VALUES (%s, %s)",
                (workspace_id, name),
            )
    return {"a": WORKSPACE_A, "b": WORKSPACE_B, "owner": int(db.create_user("v34-owner", "hash"))}


@pytest.fixture()
def project(tenants):
    return db.create_api_project(
        tenants["a"], "Billing integration", "live", created_by=tenants["owner"]
    )


def _key(project_row, workspace_id, *, public_id="pub0000000000001", **kwargs) -> dict:
    return db.create_api_key(
        project_row["id"],
        workspace_id,
        kwargs.pop("name", "server"),
        public_id,
        "hmac-digest-never-a-plaintext-key",
        "ab12",
        **kwargs,
    )


# --------------------------------------------------------------- migration --


def test_the_migration_list_ends_at_v34_and_the_test_database_is_fully_migrated():
    versions = [version for version, _ddl in db._MIGRATIONS]

    assert versions == list(range(1, 35))
    assert db.LATEST_SCHEMA_VERSION == 34
    assert db.schema_version() == 34
    # Applying an applied migration is a no-op, which is what makes the
    # startup path safe to run on every boot.
    db.init_schema()
    assert db.schema_version() == 34


def test_the_v34_migration_applies_to_a_database_that_has_never_seen_it():
    """V1..V34 against an empty database — the fresh-install path, not the
    upgrade one. CI's schema job proves the two agree; this proves the new
    migration is applicable at all, which is the half that fails at 03:00 on a
    new deployment."""
    dsn = db.dsn()
    base, _, name = dsn.rpartition("/")
    fresh_name = f"{name.split('?', 1)[0]}_v34_fresh"
    admin_dsn = f"{base}/postgres"
    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{fresh_name}"')
        admin.execute(
            f'CREATE DATABASE "{fresh_name}" TEMPLATE template0 '
            "LC_COLLATE 'C' LC_CTYPE 'C' ENCODING 'UTF8'"
        )
    try:
        with psycopg.connect(f"{base}/{fresh_name}", connect_timeout=5) as con:
            con.execute(db._MIGRATION_TABLE)
            for version, ddl in db._MIGRATIONS:
                con.execute(ddl)
                con.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                )
            con.commit()
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ).fetchall()
            }
            highest = con.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
        assert highest == 34
        assert {
            "api_projects", "api_service_accounts", "api_keys", "api_responses",
            "api_idempotency", "api_usage_minute", "api_usage_daily",
            "api_webhook_endpoints", "api_webhook_deliveries", "public_models",
            "platform_secrets",
        } <= tables
    finally:
        with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{fresh_name}"')


def test_every_v34_foreign_key_leads_an_index_because_a_cascade_walks_it():
    """The rule _MIGRATION_V34's own header states, asked of PostgreSQL rather
    than of a list of names.

    The list version of this assertion (below) passed while five foreign keys
    were unindexed — `api_webhook_deliveries.project_id` and
    `api_webhook_endpoints.workspace_id` on ON DELETE CASCADE, and three
    `created_by`/`updated_by` columns on ON DELETE SET NULL — because a set of
    names a test was told to expect can only ever restate the document it was
    copied from. This one asks `pg_constraint` for every foreign key on a V34
    table and `pg_index` whether any index LEADS with that column, which is
    the shape the cascade's lookup (and the nulling UPDATE behind SET NULL)
    can actually use. A trailing column in a composite index does not count,
    which is why `indkey[0]` and not `= ANY(indkey)`.

    Its failure mode is the one that matters: add a foreign key to V34 and
    forget its index, and this goes red naming the column, with no list to
    edit.
    """
    with db.connection() as con:
        unindexed = con.execute(
            "SELECT c.relname AS table_name, a.attname AS column_name, "
            "       con.conname AS constraint_name "
            "  FROM pg_constraint con "
            "  JOIN pg_class c ON c.oid = con.conrelid "
            "  JOIN pg_attribute a "
            "    ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1] "
            " WHERE con.contype = 'f' AND c.relname = ANY(%s) "
            "   AND NOT EXISTS (SELECT 1 FROM pg_index i "
            "                    WHERE i.indrelid = con.conrelid "
            "                      AND i.indkey[0] = con.conkey[1]) "
            " ORDER BY 1, 2",
            (list(V34_TABLES),),
        ).fetchall()
        total = con.execute(
            "SELECT count(*) AS n FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid "
            "WHERE con.contype = 'f' AND c.relname = ANY(%s)",
            (list(V34_TABLES),),
        ).fetchone()["n"]

    assert [(row["table_name"], row["column_name"]) for row in unindexed] == []
    # And the sweep really looked at something: V34 declares 24 foreign keys
    # (23, plus `public_models.workspace_id` since the overrides became per
    # workspace on 2026-09-13), so a query that silently matched no table
    # would fail here rather than passing as a vacuous truth.
    assert total == 24


def test_every_v34_table_carries_its_checks_and_the_indexes_a_cascade_needs():
    with db.connection() as con:
        checks = {
            row["conname"]
            for row in con.execute(
                "SELECT con.conname FROM pg_constraint con "
                "JOIN pg_class c ON c.oid = con.conrelid "
                "WHERE con.contype = 'c' AND c.relname = ANY(%s)",
                (list(V34_TABLES),),
            ).fetchall()
        }
        indexes = {
            row["indexname"]
            for row in con.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            ).fetchall()
        }

    assert {
        "api_projects_environment", "api_projects_status",
        "api_service_accounts_status", "api_keys_environment", "api_keys_status",
        "api_responses_status", "api_idempotency_state",
        "api_webhook_endpoints_status", "api_webhook_deliveries_status",
    } <= checks
    # The shape of every column an authorization decision is read from, and
    # the sign of every stored limit — both added to V34 in place on
    # 2026-09-13, before it had shipped anywhere.
    assert {
        "api_projects_allowed_models", "api_projects_allowed_origins",
        "api_projects_ip_allowlist", "api_projects_metadata",
        "api_service_accounts_scopes", "api_service_accounts_allowed_models",
        "api_keys_scopes", "api_keys_allowed_models",
        "api_webhook_endpoints_events", "api_webhook_deliveries_payload",
        "api_responses_metadata",
    } <= checks
    assert {
        "api_projects_rpm", "api_projects_input_tpm", "api_projects_output_tpm",
        "api_projects_max_concurrency", "api_projects_daily_token_quota",
        "api_projects_retention_days", "api_projects_max_input_tokens",
        "api_projects_max_output_tokens", "api_keys_rpm",
        "api_keys_max_concurrency", "api_responses_input_tokens",
        "api_responses_output_tokens", "api_webhook_deliveries_attempt",
        "api_webhook_deliveries_max_attempts",
        "api_webhook_endpoints_consecutive_failures",
    } <= checks
    # The ledger counters the quota is decided from (2026-09-13).
    assert {
        "api_usage_minute_requests", "api_usage_minute_input_tokens",
        "api_usage_minute_output_tokens", "api_usage_daily_requests",
        "api_usage_daily_input_tokens", "api_usage_daily_output_tokens",
        "api_usage_daily_errors", "api_usage_daily_rate_limited",
    } <= checks
    assert {
        "idx_api_projects_workspace", "idx_api_projects_created_by",
        "idx_api_projects_name", "idx_api_service_accounts_project",
        "idx_api_service_accounts_workspace", "idx_api_keys_project",
        "idx_api_keys_workspace", "idx_api_keys_service_account",
        "idx_api_keys_created_by", "idx_api_keys_revoked_by", "idx_api_keys_active",
        "idx_api_responses_project", "idx_api_responses_workspace",
        "idx_api_responses_key", "idx_api_responses_open",
        "idx_api_responses_expires", "idx_api_idempotency_claim",
        "idx_api_idempotency_expires", "idx_api_idempotency_response",
        "idx_api_usage_minute_bucket", "idx_api_usage_daily_day",
        "idx_api_webhook_endpoints_project", "idx_api_webhook_deliveries_endpoint",
        "idx_api_webhook_deliveries_due", "idx_api_webhook_deliveries_event",
        # The five the property test above found missing on 2026-09-12.
        "idx_api_webhook_deliveries_project", "idx_api_webhook_endpoints_workspace",
        "idx_api_service_accounts_created_by", "idx_api_webhook_endpoints_created_by",
        "idx_public_models_updated_by", "idx_api_keys_rotation_due",
        "idx_api_projects_one_playground",
    } <= indexes


# ----------------------------------------------------------------- projects --


def test_a_project_round_trips_with_the_limits_the_contract_promises(tenants):
    created = db.create_api_project(tenants["a"], "Default", "live")

    assert created["id"].startswith("proj_")
    assert (created["rpm"], created["input_tpm"], created["output_tpm"]) == (60, 200000, 60000)
    assert (created["max_concurrency"], created["daily_token_quota"]) == (4, 2000000)
    assert created["retention_days"] == 30 and created["status"] == "active"
    assert created["allowed_models"] == [] and created["metadata"] == {}

    assert db.get_api_project(created["id"], tenants["a"]) == created
    assert [p["id"] for p in db.list_api_projects(tenants["a"])] == [created["id"]]

    tightened = db.update_api_project(
        created["id"], tenants["a"], rpm=10, allowed_models=["techsara-35b"]
    )
    assert tightened["rpm"] == 10 and tightened["allowed_models"] == ["techsara-35b"]

    disabled = db.update_api_project(created["id"], tenants["a"], status="disabled")
    assert disabled["status"] == "disabled" and disabled["disabled_at"] is not None
    assert db.list_api_projects(tenants["a"], include_disabled=False) == []


def test_a_project_may_not_change_its_environment_or_its_workspace(project, tenants):
    for column in ("environment", "workspace_id", "id", "created_by"):
        with pytest.raises(ValueError):
            db.update_api_project(project["id"], tenants["a"], **{column: "live"})


def test_a_project_name_is_unique_per_workspace_and_free_in_the_next_one(tenants):
    db.create_api_project(tenants["a"], "Shared name", "live")
    with pytest.raises(db.IntegrityError):
        db.create_api_project(tenants["a"], "SHARED NAME", "test")
    assert db.create_api_project(tenants["b"], "Shared name", "live")["id"]


def test_an_unknown_environment_is_a_caller_mistake_not_a_constraint_violation(tenants):
    with pytest.raises(ValueError):
        db.create_api_project(tenants["a"], "Staging", "staging")


# ------------------------------------------------------- shapes and limits --


def test_an_allowlist_that_is_not_a_list_is_refused_rather_than_reshaped(project, tenants):
    """CONTRACT-3 §3 and §7 make authorization decisions out of these columns,
    so a value no reader can evaluate must not reach them.

    Measured on 2026-09-12, before this: `allowed_models='techsara-35b'` was
    stored as `{"raw": "techsara-35b"}` (a reader asking `model in …` then
    tests the object's KEYS), `allowed_origins={'x': 1}` was stored verbatim,
    and `create_api_project(..., allowed_models='abc')` was stored as
    `['a','b','c']` because of `list(value)` — an allowlist of three letters
    that looks like a list to every reader downstream.
    """
    for bad in ("techsara-35b", {"x": 1}, 7, [None], ["ok", 3]):
        with pytest.raises(ValueError):
            db.create_api_project(tenants["a"], f"proj {bad!r}", "live", allowed_models=bad)
        with pytest.raises(ValueError):
            db.update_api_project(project["id"], tenants["a"], allowed_models=bad)
        with pytest.raises(ValueError):
            db.update_api_project(project["id"], tenants["a"], allowed_origins=bad)
        with pytest.raises(ValueError):
            db.update_api_project(project["id"], tenants["a"], ip_allowlist=bad)
        with pytest.raises(ValueError):
            db.create_service_account(project["id"], tenants["a"], f"svc {bad!r}", scopes=bad)
        with pytest.raises(ValueError):
            db.create_webhook_endpoint(
                project["id"], tenants["a"], "https://example.test/hook", bad, "shh"
            )

    # Nothing was written by any of the refusals.
    assert db.get_api_project(project["id"], tenants["a"])["allowed_models"] == []
    assert db.list_service_accounts(project["id"], tenants["a"]) == []
    assert db.list_webhook_endpoints(project["id"], tenants["a"]) == []
    # And the good shapes still work, a set arriving sorted so a console does
    # not reorder scopes between two page loads.
    stored = db.update_api_project(
        project["id"], tenants["a"], allowed_models=("techsara-35b",), allowed_origins=[]
    )
    assert stored["allowed_models"] == ["techsara-35b"] and stored["allowed_origins"] == []
    account = db.create_service_account(
        project["id"], tenants["a"], "sorted", scopes={"responses.write", "models.read"}
    )
    assert account["scopes"] == ["models.read", "responses.write"]


def test_an_object_column_refuses_a_list_because_the_page_that_reads_it_would_fail(
    project, tenants
):
    with pytest.raises(ValueError):
        db.update_api_project(project["id"], tenants["a"], metadata=["not", "an", "object"])
    with pytest.raises(ValueError):
        db.create_api_project(tenants["a"], "Listy", "live", metadata=[1, 2])

    response = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1")
    with pytest.raises(ValueError):
        db.update_api_response(response["id"], project["id"], metadata=["x"])
    assert db.get_api_response(response["id"], project["id"])["metadata"] == {}


def test_the_database_itself_refuses_a_scalar_in_an_allowlist_column(project, tenants):
    """The Python guard above is the good error message; this CHECK is the
    one that holds when the next writer is a migration, a fixture or psql."""
    # `pytest.raises` sits OUTSIDE `db.connection()` so the context manager
    # sees the failure and rolls back, rather than trying to commit a
    # transaction PostgreSQL has already aborted.
    with pytest.raises(db.IntegrityError):
        with db.connection() as con:
            con.execute(
                "UPDATE api_projects SET allowed_models = '\"techsara-35b\"'::jsonb "
                "WHERE id = %s",
                (project["id"],),
            )
    with pytest.raises(db.IntegrityError):
        with db.connection() as con:
            con.execute(
                "UPDATE api_projects SET metadata = '[]'::jsonb WHERE id = %s",
                (project["id"],),
            )
    assert db.get_api_project(project["id"], tenants["a"])["allowed_models"] == []


def test_a_negative_limit_cannot_be_stored_so_a_response_is_never_born_expired(
    project, tenants
):
    """The incident this closes, measured on 2026-09-12: `retention_days=-5`
    was accepted, every response created afterwards got an `expires_at` five
    days in the PAST (`now() + make_interval(days => p.retention_days)`), and
    the next `prune_api_platform` sweep deleted a background response before
    the caller that started it could fetch it."""
    for column in (
        "retention_days", "rpm", "input_tpm", "output_tpm", "max_concurrency",
        "daily_token_quota",
    ):
        with pytest.raises(db.IntegrityError):
            db.update_api_project(project["id"], tenants["a"], **{column: -1})
    for column in ("max_input_tokens", "max_output_tokens"):
        with pytest.raises(db.IntegrityError):
            db.update_api_project(project["id"], tenants["a"], **{column: -1})
    with pytest.raises(db.IntegrityError):
        db.create_api_project(tenants["a"], "Backwards", "live", retention_days=-5)

    # The project still holds its defaults, so a response created now expires
    # in the future.
    assert db.get_api_project(project["id"], tenants["a"])["retention_days"] == 30
    response = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1")
    assert response["expires_at"] > response["created_at"]
    assert db.prune_api_platform()["api_responses"] == 0
    assert db.get_api_response(response["id"], project["id"]) is not None

    # Zero is not negative: a frozen project is a real operator choice, and
    # `retention_days = 0` is what the retention test below relies on. Zero
    # means ZERO for every limit column, the token ceilings included — only
    # NULL inherits (2026-09-13; the resolver had been reading a stored 0 as
    # "use the default" and admitted 60 requests to a frozen project).
    frozen = db.update_api_project(
        project["id"], tenants["a"], rpm=0, daily_token_quota=0, retention_days=0,
        max_input_tokens=0, max_output_tokens=0, max_concurrency=0,
    )
    assert (
        frozen["rpm"], frozen["daily_token_quota"], frozen["retention_days"],
        frozen["max_input_tokens"], frozen["max_output_tokens"], frozen["max_concurrency"],
    ) == (0, 0, 0, 0, 0, 0)
    inherited = db.update_api_project(project["id"], tenants["a"], max_input_tokens=None)
    assert inherited["max_input_tokens"] is None


def test_a_key_limit_and_a_delivery_budget_are_bounded_by_the_schema(project, tenants):
    with pytest.raises(db.IntegrityError):
        _key(project, tenants["a"], rpm=-1)
    with pytest.raises(db.IntegrityError):
        _key(project, tenants["a"], public_id="pub0000000000009", max_concurrency=-1)

    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook", ["response.completed"], "shh"
    )
    # A delivery with no attempts is never due and never fails — it is
    # silently dropped, which is the one outcome a delivery history exists to
    # make impossible.
    with pytest.raises(db.IntegrityError):
        db.enqueue_webhook_delivery(
            endpoint["id"], project["id"], "response.completed", "evt_0", {}, max_attempts=0
        )

    response = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1")
    with pytest.raises(db.IntegrityError):
        db.update_api_response(response["id"], project["id"], output_tokens=-100)


# ---------------------------------------------------------------- isolation --


def test_a_project_of_workspace_a_is_invisible_to_workspace_b(project, tenants):
    assert db.get_api_project(project["id"], tenants["b"]) is None
    assert db.list_api_projects(tenants["b"]) == []
    assert db.update_api_project(project["id"], tenants["b"], rpm=1) is None
    # …and the attempt changed nothing.
    assert db.get_api_project(project["id"], tenants["a"])["rpm"] == 60


def test_a_key_of_workspace_a_is_invisible_and_unrevokable_from_workspace_b(project, tenants):
    key = _key(project, tenants["a"])

    assert db.list_api_keys(project["id"], tenants["b"]) == []
    assert db.revoke_api_key(key["id"], tenants["b"]) is None
    assert db.api_key_by_public_id(key["public_id"])["status"] == "active"


def test_a_service_account_of_workspace_a_is_invisible_through_workspace_b(project, tenants):
    """The mutation this pins: delete `AND workspace_id = %s` from
    `list_service_accounts` and every other test in this file still passes,
    because none of them ever asked as the wrong tenant. Project ids are
    CSPRNG, so this is a second lock rather than the only one — but the whole
    point of rule 1 in db.py is that the predicate is in the statement and not
    in a handler that might forget it."""
    account = db.create_service_account(project["id"], tenants["a"], "nightly-sync")

    assert [a["id"] for a in db.list_service_accounts(project["id"], tenants["a"])] == [
        account["id"]
    ]
    assert db.list_service_accounts(project["id"], tenants["b"]) == []
    assert db.get_service_account(account["id"], tenants["a"])["name"] == "nightly-sync"
    assert db.get_service_account(account["id"], tenants["b"]) is None
    assert db.set_service_account_status(account["id"], tenants["b"], "disabled") is None
    # …and the attempt changed nothing.
    assert db.get_service_account(account["id"], tenants["a"])["status"] == "active"


def test_a_webhook_endpoint_of_workspace_a_is_invisible_through_workspace_b(project, tenants):
    """The same mutation, on the function whose rows carry a live signing
    secret: `list_webhook_endpoints` was called three times in this file and
    always as its owner."""
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook",
        ["response.completed"], "shh",
    )

    assert [e["id"] for e in db.list_webhook_endpoints(project["id"], tenants["a"])] == [
        endpoint["id"]
    ]
    assert db.list_webhook_endpoints(project["id"], tenants["b"]) == []
    assert db.get_webhook_endpoint(endpoint["id"], tenants["a"])["id"] == endpoint["id"]
    assert db.get_webhook_endpoint(endpoint["id"], tenants["b"]) is None


def test_a_response_of_one_project_is_invisible_through_another(project, tenants):
    other = db.create_api_project(tenants["b"], "Beta project", "live")
    response = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1")

    assert db.get_api_response(response["id"], other["id"]) is None
    assert db.update_api_response(response["id"], other["id"], status="completed") is None
    assert db.list_api_responses(other["id"]) == []
    assert db.get_api_response(response["id"], project["id"])["status"] == "queued"


def test_a_child_may_not_be_created_under_another_workspaces_project(project, tenants):
    with pytest.raises(ValueError):
        db.create_service_account(project["id"], tenants["b"], "smuggled")
    with pytest.raises(ValueError):
        _key(project, tenants["b"])
    with pytest.raises(ValueError):
        db.create_api_response(project["id"], tenants["b"], "techsara-35b", "req_x")
    with pytest.raises(ValueError):
        db.create_webhook_endpoint(
            project["id"], tenants["b"], "https://example.test/hook", ["response.completed"], "s"
        )


# ------------------------------------------------- service accounts and keys --


def test_a_service_account_and_its_key_inherit_the_projects_tenancy(project, tenants):
    account = db.create_service_account(
        project["id"], tenants["a"], "nightly-sync",
        description="batch", scopes=["responses.write"], created_by=tenants["owner"],
    )
    assert account["id"].startswith("svc_")
    assert account["workspace_id"] == tenants["a"] and account["status"] == "active"
    assert [a["id"] for a in db.list_service_accounts(project["id"], tenants["a"])] == [account["id"]]

    key = _key(project, tenants["a"], service_account_id=account["id"], scopes=["responses.write"])
    assert key["id"].startswith("key_")
    # The environment is the PROJECT's, never the caller's assertion.
    assert key["environment"] == "live" and key["workspace_id"] == tenants["a"]
    assert key["last_four"] == "ab12" and key["rpm"] is None


def test_a_key_cannot_borrow_a_service_account_from_another_project(project, tenants):
    elsewhere = db.create_api_project(tenants["a"], "Other", "live")
    foreign = db.create_service_account(elsewhere["id"], tenants["a"], "foreign")

    with pytest.raises(ValueError):
        _key(project, tenants["a"], service_account_id=foreign["id"])


def test_a_key_refuses_to_claim_an_environment_its_project_does_not_have(project, tenants):
    with pytest.raises(ValueError):
        _key(project, tenants["a"], environment="test")


def test_resolving_a_key_by_public_id_brings_its_project_and_service_account(project, tenants):
    account = db.create_service_account(project["id"], tenants["a"], "nightly")
    key = _key(project, tenants["a"], service_account_id=account["id"])

    resolved = db.api_key_by_public_id(key["public_id"])

    assert resolved["id"] == key["id"]
    assert resolved["project"]["id"] == project["id"]
    assert resolved["project"]["workspace_id"] == tenants["a"]
    assert resolved["service_account"]["id"] == account["id"]
    assert db.api_key_by_public_id("pub-that-was-never-issued") is None


def test_a_revoked_key_still_resolves_so_the_surface_can_answer_401_the_same_way(project, tenants):
    key = _key(project, tenants["a"])

    revoked = db.revoke_api_key(key["id"], tenants["a"], revoked_by=tenants["owner"])
    assert revoked["status"] == "revoked" and revoked["revoked_by"] == tenants["owner"]

    resolved = db.api_key_by_public_id(key["public_id"])
    assert resolved is not None and resolved["status"] == "revoked"
    assert db.list_api_keys(project["id"], tenants["a"], include_revoked=False) == []


def test_revoking_twice_keeps_the_first_revocation_and_its_actor(project, tenants):
    key = _key(project, tenants["a"])
    first = db.revoke_api_key(key["id"], tenants["a"], revoked_by=tenants["owner"])

    again = db.revoke_api_key(key["id"], tenants["a"], revoked_by=None)

    assert again["revoked_at"] == first["revoked_at"]
    assert again["revoked_by"] == tenants["owner"]


def test_touching_a_key_records_use_and_leaves_the_last_address_alone(project, tenants):
    account = db.create_service_account(project["id"], tenants["a"], "nightly")
    key = _key(project, tenants["a"], service_account_id=account["id"])

    db.touch_api_key(key["id"], "203.0.113.7")
    db.touch_api_key(key["id"])  # a request with no address must not erase one

    touched = db.api_key_by_public_id(key["public_id"])
    assert touched["last_used_ip"] == "203.0.113.7"
    assert touched["last_used_at"] is not None
    assert touched["service_account"]["last_used_at"] is not None


def test_no_list_read_of_a_key_carries_the_digest_that_verifies_it(project, tenants):
    """`list_api_keys`' docstring said "no secret material beyond last_four is
    ever readable, because none is stored", and `SELECT *` was returning
    `key_hash` — the HMAC the resolver compares a presented secret against.

    Nothing had leaked, because no route consumed the function yet. That is
    exactly why it mattered: the layer was telling the next wave's author that
    serialising these rows into a console page was safe.
    """
    key = _key(project, tenants["a"], created_by=tenants["owner"])

    listed = db.list_api_keys(project["id"], tenants["a"])[0]
    revoked = db.revoke_api_key(key["id"], tenants["a"], revoked_by=tenants["owner"])

    assert "key_hash" not in listed and "key_hash" not in revoked
    # Everything a console needs is still there…
    assert listed["last_four"] == "ab12" and listed["public_id"] == key["public_id"]
    assert listed["name"] == "server" and listed["status"] == "active"
    assert revoked["status"] == "revoked"
    # …and the one caller that genuinely needs the digest still gets it,
    # because verifying a presented secret is impossible without it.
    assert db.api_key_by_public_id(key["public_id"])["key_hash"] == (
        "hmac-digest-never-a-plaintext-key"
    )


def test_disabling_a_service_account_stops_its_keys_without_revoking_them(project, tenants):
    """The three-in-the-morning switch. CONTRACT-3 §4 resolves the service
    account on every `/v1` request and answers 401 when it is disabled, so
    this one row stops every key hanging off it at once — and re-enabling
    brings the same integration back without the customer redeploying a new
    credential. Revocation is the other operation, for the case where the
    credential itself is the problem."""
    account = db.create_service_account(project["id"], tenants["a"], "nightly-sync")
    key = _key(project, tenants["a"], service_account_id=account["id"])

    disabled = db.set_service_account_status(account["id"], tenants["a"], "disabled")

    assert disabled["status"] == "disabled" and disabled["disabled_at"] is not None
    # The key is untouched: the resolver refuses it because of the ACCOUNT.
    resolved = db.api_key_by_public_id(key["public_id"])
    assert resolved["status"] == "active"
    assert resolved["service_account"]["status"] == "disabled"

    again = db.set_service_account_status(account["id"], tenants["a"], "disabled")
    assert again["disabled_at"] == disabled["disabled_at"]

    back = db.set_service_account_status(account["id"], tenants["a"], "active")
    assert back["status"] == "active" and back["disabled_at"] is None
    assert db.api_key_by_public_id(key["public_id"])["service_account"]["status"] == "active"

    with pytest.raises(ValueError):
        db.set_service_account_status(account["id"], tenants["a"], "deleted")
    assert db.set_service_account_status("svc_never_issued", tenants["a"], "disabled") is None


def test_a_rotated_out_key_dies_when_its_own_overlap_expires_and_not_before(project, tenants):
    """An overlap is two live credentials by construction — that is what makes
    a rotation deployable without an outage — and the only thing separating
    that from "the old key never died" is somebody coming back. This sweep is
    the coming back.

    Which row carries what, settled 2026-09-13: the NEW key carries
    `rotated_from` (history); the OLD key carries `rotation_expires_at` (its
    own deadline), written by `update_api_key_rotation`. That is how the
    resolver has always read the column. The earlier version of this test
    built the deadline onto the successor by hand — a row no code path
    produced — and passed while no overlap was ever enforced in production.
    """
    now = datetime.now(timezone.utc)
    old = _key(project, tenants["a"], public_id="pub0000000000001", name="old")
    new = _key(
        project, tenants["a"], public_id="pub0000000000002", name="new",
        rotated_from=old["public_id"],
    )
    untouched = _key(project, tenants["a"], public_id="pub0000000000003", name="unrelated")

    dated = db.update_api_key_rotation(old["id"], tenants["a"], now + timedelta(days=7))
    assert dated["id"] == old["id"] and dated["rotation_expires_at"] is not None
    assert "key_hash" not in dated
    # The replacement carries history, never a deadline of its own.
    assert new["rotated_from"] == old["public_id"] and new["rotation_expires_at"] is None

    # Inside the grace window both credentials still work, which is the whole
    # reason the window exists.
    assert db.expire_rotated_keys(now=now) == []
    assert db.api_key_by_public_id(old["public_id"])["status"] == "active"

    expired = db.expire_rotated_keys(now=now + timedelta(days=7, seconds=1))

    assert [k["id"] for k in expired] == [old["id"]]
    assert "key_hash" not in expired[0]
    assert db.api_key_by_public_id(old["public_id"])["status"] == "revoked"
    # The replacement is the key the integration now runs on.
    assert db.api_key_by_public_id(new["public_id"])["status"] == "active"
    # Nobody did this, so nobody is recorded as having done it — but the
    # moment is, exactly as for a revocation somebody clicked.
    assert expired[0]["revoked_by"] is None and expired[0]["revoked_at"] is not None
    assert db.api_key_by_public_id(untouched["public_id"])["status"] == "active"
    # Idempotent: the second sweep finds nothing left to do.
    assert db.expire_rotated_keys(now=now + timedelta(days=30)) == []


def test_a_zero_overlap_revocation_is_swept_on_the_first_run(project, tenants):
    """`plan_revocation` is `plan_rotation` with zero overlap — the compromise
    and the departing-colleague case — so the predecessor must be dead at the
    first sweep after its deadline is written, and the brand-new replacement
    must NOT be: the old draft that wrote the deadline onto the successor
    would have refused the new key the instant it was minted."""
    now = datetime.now(timezone.utc)
    old = _key(project, tenants["a"], public_id="pub0000000000001", name="compromised")
    _key(
        project, tenants["a"], public_id="pub0000000000002", name="replacement",
        rotated_from=old["public_id"],
    )
    db.update_api_key_rotation(old["id"], tenants["a"], now)

    assert [k["id"] for k in db.expire_rotated_keys(now=now)] == [old["id"]]
    assert [k["name"] for k in db.list_api_keys(project["id"], tenants["a"], include_revoked=False)] == [
        "replacement"
    ]


def test_a_rotation_deadline_can_only_be_written_by_the_workspace_that_owns_the_key(
    project, tenants
):
    key = _key(project, tenants["a"])
    deadline = datetime.now(timezone.utc) + timedelta(days=1)

    assert db.update_api_key_rotation(key["id"], tenants["b"], deadline) is None
    assert db.api_key_by_public_id(key["public_id"])["rotation_expires_at"] is None
    assert db.update_api_key_rotation("key_never_issued", tenants["a"], deadline) is None

    # None clears a planned cut-over, and dating a key never changes its status.
    db.update_api_key_rotation(key["id"], tenants["a"], deadline)
    cleared = db.update_api_key_rotation(key["id"], tenants["a"], None)
    assert cleared["rotation_expires_at"] is None and cleared["status"] == "active"
    db.revoke_api_key(key["id"], tenants["a"])
    assert db.update_api_key_rotation(key["id"], tenants["a"], deadline)["status"] == "revoked"


def test_a_key_in_workspace_b_cannot_name_workspace_as_key_as_its_predecessor(project, tenants):
    """The cross-tenant revocation of 2026-09-13, reproduced as it was found.

    `create_api_key` stored any `rotated_from` it was handed, and the sweep
    joined `successor.rotated_from = previous.public_id` across every
    workspace — so workspace B, knowing only A's public id (which CONTRACT §5
    calls safe to log), could get A's live key revoked. Two independent
    closures, both pinned: the write refuses a predecessor outside the key's
    own project, and the sweep no longer follows the reference at all — even a
    row written straight into the table (psql, a future accessor) revokes
    nothing but itself.
    """
    victim = _key(project, tenants["a"], public_id="pubVICTIM00000001")
    attacker_project = db.create_api_project(tenants["b"], "Attacker", "live")
    now = datetime.now(timezone.utc)

    with pytest.raises(ValueError, match="rotated_from"):
        _key(
            attacker_project, tenants["b"], public_id="pubATTACK00000001",
            rotated_from=victim["public_id"],
        )
    # A caller still passing the old argument is told, not silently obeyed.
    with pytest.raises(TypeError):
        _key(
            attacker_project, tenants["b"], public_id="pubATTACK00000002",
            rotation_expires_at=now,
        )
    # Another project in the SAME workspace is still another project.
    sibling = db.create_api_project(tenants["a"], "Sibling", "live")
    with pytest.raises(ValueError, match="rotated_from"):
        _key(sibling, tenants["a"], public_id="pubSIBLING0000001", rotated_from=victim["public_id"])
    assert db.list_api_keys(sibling["id"], tenants["a"]) == []
    assert db.list_api_keys(attacker_project["id"], tenants["b"]) == []

    # The sweep side, with the row forged below the accessor.
    attacker = _key(attacker_project, tenants["b"], public_id="pubATTACK00000003")
    with db.connection() as con:
        con.execute(
            "UPDATE api_keys SET rotated_from = %s, rotation_expires_at = %s WHERE id = %s",
            (victim["public_id"], now - timedelta(seconds=1), attacker["id"]),
        )

    swept = db.expire_rotated_keys(now=now)

    assert [(k["id"], k["workspace_id"]) for k in swept] == [(attacker["id"], tenants["b"])]
    assert db.api_key_by_public_id(victim["public_id"])["status"] == "active"

# ---------------------------------------------------------------- responses --


def test_a_response_round_trips_and_its_timestamps_follow_its_status(project, tenants):
    key = _key(project, tenants["a"])
    response = db.create_api_response(
        project["id"], tenants["a"], "techsara-35b", "req_42",
        key_id=key["id"], background=True, fingerprint="fp-1",
        metadata={"customer_request_id": "abc-123"},
    )

    assert response["id"].startswith("resp_")
    assert response["status"] == "queued" and response["background"] is True
    assert response["input_tokens"] is None and response["output_tokens"] is None
    assert response["metadata"] == {"customer_request_id": "abc-123"}
    # Retention is the project's, applied without the caller having to know it.
    assert response["expires_at"] is not None

    running = db.update_api_response(response["id"], project["id"], status="in_progress")
    assert running["started_at"] is not None and running["completed_at"] is None

    done = db.update_api_response(
        response["id"], project["id"], status="completed",
        input_tokens=37, output_tokens=112, output_text="…", ttft_ms=120, duration_ms=900,
    )
    assert done["completed_at"] is not None and done["output_tokens"] == 112
    assert [r["id"] for r in db.list_api_responses(project["id"], status="completed")] == [response["id"]]
    assert db.list_api_responses(project["id"], status="failed") == []


def test_a_response_may_not_be_moved_between_tenants_by_an_update(project, tenants):
    response = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1")
    for column in ("project_id", "workspace_id", "key_id", "model"):
        with pytest.raises(ValueError):
            db.update_api_response(response["id"], project["id"], **{column: "x"})


def test_the_response_page_size_is_clamped_however_large_the_caller_asks(project, tenants):
    for index in range(3):
        db.create_api_response(project["id"], tenants["a"], "techsara-35b", f"req_{index}")

    assert len(db.list_api_responses(project["id"], limit=10**9)) == 3
    assert len(db.list_api_responses(project["id"], limit=0)) == 1


# -------------------------------------------------------------- idempotency --


def test_claim_idempotency_returns_a_row_once_and_none_to_every_later_claimant(project, tenants):
    first = db.claim_idempotency(project["id"], "/v1/responses", "idem-1", "fingerprint-a")
    second = db.claim_idempotency(project["id"], "/v1/responses", "idem-1", "fingerprint-a")
    different_body = db.claim_idempotency(project["id"], "/v1/responses", "idem-1", "fingerprint-b")

    assert first is not None and first["state"] == "in_flight"
    assert second is None and different_body is None
    # The loser reads the claim and sees the fingerprint it must compare
    # against — that comparison is what makes a 409 rather than a replay.
    assert db.get_idempotency(project["id"], "/v1/responses", "idem-1")["fingerprint"] == "fingerprint-a"


def test_the_same_idempotency_key_is_free_in_another_project_and_on_another_endpoint(project, tenants):
    other = db.create_api_project(tenants["b"], "Beta", "live")
    db.claim_idempotency(project["id"], "/v1/responses", "idem-1", "fp")

    assert db.claim_idempotency(other["id"], "/v1/responses", "idem-1", "fp") is not None
    assert db.claim_idempotency(project["id"], "/v1/chat/completions", "idem-1", "fp") is not None
    assert db.get_idempotency(other["id"], "/v1/responses", "idem-1")["project_id"] == other["id"]


def test_only_one_of_many_concurrent_claimants_wins_the_key(project, tenants):
    ready = threading.Barrier(8)
    won: list = []

    def claim() -> None:
        ready.wait(timeout=10)
        if db.claim_idempotency(project["id"], "/v1/responses", "race", "fp") is not None:
            won.append(1)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sum(won) == 1


def test_finishing_a_claim_attaches_the_response_a_retry_should_replay(project, tenants):
    response = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1")
    db.claim_idempotency(project["id"], "/v1/responses", "idem-2", "fp")

    finished = db.finish_idempotency(project["id"], "/v1/responses", "idem-2", response["id"])

    assert finished["state"] == "completed" and finished["response_id"] == response["id"]
    # A REAL second project, not a workspace id in the project_id slot. The
    # earlier version of this line passed `tenants["b"]` — a workspace — which
    # only proved that an id matching no project returns None, and would have
    # passed just as happily with the project predicate deleted.
    neighbour = db.create_api_project(tenants["b"], "Beta", "live")
    assert db.finish_idempotency(
        neighbour["id"], "/v1/responses", "idem-2", response["id"]
    ) is None
    assert db.get_idempotency(neighbour["id"], "/v1/responses", "idem-2") is None
    # …and the real claim is untouched by the neighbour's attempt.
    assert db.get_idempotency(project["id"], "/v1/responses", "idem-2")["state"] == "completed"


# --------------------------------------------------------------------- usage --


def test_usage_bumps_accumulate_and_the_window_reads_zero_before_anything_happens(project, tenants):
    key = _key(project, tenants["a"])

    empty = db.usage_window(project["id"], key["id"])
    assert empty["minute"]["requests"] == 0 and empty["daily"]["input_tokens"] == 0

    db.bump_usage_minute(project["id"], key["id"], requests=1, input_tokens=100, output_tokens=20)
    db.bump_usage_minute(project["id"], key["id"], requests=1, input_tokens=50, output_tokens=5)
    db.bump_usage_daily(project["id"], requests=2, input_tokens=150, output_tokens=25, errors=1)

    window = db.usage_window(project["id"], key["id"])
    assert window["minute"] == {"requests": 2, "input_tokens": 150, "output_tokens": 25}
    assert window["daily"]["requests"] == 2 and window["daily"]["errors"] == 1
    assert window["daily"]["rate_limited"] == 0


def test_a_previous_minute_does_not_count_against_this_one(project, tenants):
    key = _key(project, tenants["a"])
    now = datetime.now(timezone.utc)

    db.bump_usage_minute(project["id"], key["id"], requests=9, at=now - timedelta(minutes=1))
    db.bump_usage_minute(project["id"], key["id"], requests=1, at=now)

    assert db.usage_window(project["id"], key["id"], at=now)["minute"]["requests"] == 1


def test_the_window_reports_the_previous_bucket_so_a_sliding_limit_can_be_derived(
    project, tenants
):
    """CONTRACT-3 §12 promises "sliding window, durable" and V34 stores fixed
    minute buckets. That is not a disagreement: the standard approximation
    reads the current bucket and weights the previous one by how much of it is
    still inside the last 60 seconds —

        estimate = current + previous * (60 - elapsed) / 60

    — which is durable (both rows are in PostgreSQL and survive a restart) and
    costs one extra row in the same index scan. No table, column or second
    ledger is needed for §12; what was needed was this read returning the
    second row, because `app/db.py` is single-owner and the quota engine
    cannot add its own SQL.

    The property the estimate exists for: a fixed bucket lets a caller spend
    the whole minute's allowance twice in two seconds either side of a
    boundary. Here, 15 seconds into the new minute, 45/60 of the old minute's
    spend still counts.
    """
    key = _key(project, tenants["a"])
    now = datetime.now(timezone.utc).replace(second=15, microsecond=0)

    db.bump_usage_minute(project["id"], key["id"], requests=60, at=now - timedelta(minutes=1))
    db.bump_usage_minute(project["id"], key["id"], requests=10, at=now)

    window = db.usage_window(project["id"], key["id"], at=now)

    assert window["minute"]["requests"] == 10
    assert window["previous"]["requests"] == 60
    assert window["elapsed_seconds"] == 15.0
    estimate = (
        window["minute"]["requests"]
        + window["previous"]["requests"] * (60 - window["elapsed_seconds"]) / 60
    )
    assert estimate == 55.0
    # A minute later the old bucket has slid out of view entirely, without
    # anything being deleted.
    later = db.usage_window(project["id"], key["id"], at=now + timedelta(minutes=1))
    assert later["minute"]["requests"] == 0 and later["previous"]["requests"] == 10


def test_one_keys_usage_is_not_charged_to_another(project, tenants):
    first = _key(project, tenants["a"], public_id="pub0000000000001")
    second = _key(project, tenants["a"], public_id="pub0000000000002")

    db.bump_usage_minute(project["id"], first["id"], requests=5)

    assert db.usage_window(project["id"], first["id"])["minute"]["requests"] == 5
    assert db.usage_window(project["id"], second["id"])["minute"]["requests"] == 0


def test_concurrent_usage_bumps_all_land_because_the_increment_is_one_statement(project, tenants):
    """The lost-update test. Eight threads, twenty-five bumps each: a
    read-then-write would report far fewer than two hundred, and the
    difference is quota somebody else gets to spend."""
    key = _key(project, tenants["a"])
    ready = threading.Barrier(8)
    at = datetime.now(timezone.utc)
    errors: list = []

    def spend() -> None:
        try:
            ready.wait(timeout=10)
            for _ in range(25):
                db.bump_usage_minute(
                    project["id"], key["id"], requests=1, input_tokens=10, at=at
                )
                db.bump_usage_daily(project["id"], requests=1, input_tokens=10, day=at)
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=spend) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    window = db.usage_window(project["id"], key["id"], at=at)
    assert window["minute"] == {"requests": 200, "input_tokens": 2000, "output_tokens": 0}
    assert window["daily"]["requests"] == 200 and window["daily"]["input_tokens"] == 2000


def test_read_usage_daily_returns_an_inclusive_range_and_refuses_an_unbounded_one(project, tenants):
    today = datetime.now(timezone.utc).date()
    for offset in range(4):
        db.bump_usage_daily(project["id"], requests=offset + 1, day=today - timedelta(days=offset))

    rows = db.read_usage_daily(project["id"], today - timedelta(days=2), today)

    assert [row["day"] for row in rows] == [
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    assert rows[-1]["requests"] == 1
    # Strings are accepted because that is what arrives on the wire.
    assert db.read_usage_daily(project["id"], today.isoformat(), today.isoformat())
    with pytest.raises(ValueError):
        db.read_usage_daily(project["id"], today, today - timedelta(days=1))
    with pytest.raises(ValueError):
        db.read_usage_daily(project["id"], today - timedelta(days=400), today)


# ------------------------------------------------------------------ webhooks --


def test_a_webhook_endpoint_round_trips_and_refuses_a_plaintext_target(project, tenants):
    with pytest.raises(ValueError):
        db.create_webhook_endpoint(
            project["id"], tenants["a"], "http://example.test/hook", ["response.completed"], "shh"
        )

    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook",
        ["response.completed", "response.failed"], "shh", created_by=tenants["owner"],
    )
    assert endpoint["id"].startswith("whe_")
    assert endpoint["events"] == ["response.completed", "response.failed"]
    assert endpoint["status"] == "active" and endpoint["consecutive_failures"] == 0

    rotated = db.update_webhook_endpoint(
        endpoint["id"], tenants["a"], secret="new", previous_secret="shh",
        previous_secret_expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    # The rotation lands — the sweep signs with it — but is not echoed back.
    assert rotated["has_secret"] is True and rotated["has_previous_secret"] is True
    assert "secret" not in rotated and "previous_secret" not in rotated
    db.enqueue_webhook_delivery(endpoint["id"], project["id"], "response.completed", "evt_r", {})
    due = db.due_webhook_deliveries()[0]["endpoint"]
    assert (due["secret"], due["previous_secret"]) == ("new", "shh")

    assert db.update_webhook_endpoint(endpoint["id"], tenants["b"], status="disabled") is None
    assert db.delete_webhook_endpoint(endpoint["id"], tenants["b"]) is False
    assert db.delete_webhook_endpoint(endpoint["id"], tenants["a"]) is True
    assert db.list_webhook_endpoints(project["id"], tenants["a"]) == []


def test_an_event_is_queued_to_an_endpoint_once_and_carries_it_to_the_sweep(project, tenants):
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook", ["response.completed"], "shh"
    )
    response = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1")

    queued = db.enqueue_webhook_delivery(
        endpoint["id"], project["id"], "response.completed", "evt_1",
        {"id": response["id"]}, response_id=response["id"],
    )
    duplicate = db.enqueue_webhook_delivery(
        endpoint["id"], project["id"], "response.completed", "evt_1", {"id": response["id"]}
    )

    assert queued["id"].startswith("whd_") and queued["status"] == "pending"
    assert queued["attempt"] == 0 and queued["max_attempts"] == 6
    assert duplicate is None

    due = db.due_webhook_deliveries()
    assert [d["id"] for d in due] == [queued["id"]]
    assert due[0]["endpoint"]["url"] == "https://example.test/hook"
    assert due[0]["endpoint"]["secret"] == "shh"

    with pytest.raises(ValueError):
        db.enqueue_webhook_delivery(
            endpoint["id"], "proj_not_this_one", "response.completed", "evt_2", {}
        )


def test_a_failed_attempt_is_counted_rescheduled_and_then_forgiven(project, tenants):
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook", ["response.completed"], "shh"
    )
    delivery = db.enqueue_webhook_delivery(
        endpoint["id"], project["id"], "response.completed", "evt_1", {}
    )

    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    failed = db.record_webhook_attempt(
        delivery["id"], status="pending", http_status=500, error="upstream said no",
        next_attempt_at=later,
    )
    assert failed["attempt"] == 1 and failed["delivered_at"] is None
    assert db.list_webhook_endpoints(project["id"], tenants["a"])[0]["consecutive_failures"] == 1
    # Not due again until its backoff has elapsed.
    assert db.due_webhook_deliveries() == []
    assert [d["id"] for d in db.due_webhook_deliveries(now=later)] == [delivery["id"]]

    delivered = db.record_webhook_attempt(delivery["id"], status="delivered", http_status=200)
    assert delivered["attempt"] == 2 and delivered["delivered_at"] is not None
    endpoint_after = db.list_webhook_endpoints(project["id"], tenants["a"])[0]
    assert endpoint_after["consecutive_failures"] == 0
    assert endpoint_after["last_delivery_status"] == "delivered"
    assert db.due_webhook_deliveries(now=later) == []

    with pytest.raises(ValueError):
        db.record_webhook_attempt(delivery["id"], status="exploded")


def test_a_disabled_endpoints_queue_is_paused_rather_than_lost(project, tenants):
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook", ["response.completed"], "shh"
    )
    delivery = db.enqueue_webhook_delivery(
        endpoint["id"], project["id"], "response.completed", "evt_1", {}
    )

    db.update_webhook_endpoint(endpoint["id"], tenants["a"], status="disabled")
    assert db.due_webhook_deliveries() == []

    db.update_webhook_endpoint(endpoint["id"], tenants["a"], status="active")
    assert [d["id"] for d in db.due_webhook_deliveries()] == [delivery["id"]]


def test_a_delivery_that_has_spent_its_attempts_is_never_due_again(project, tenants):
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook", ["response.completed"], "shh"
    )
    delivery = db.enqueue_webhook_delivery(
        endpoint["id"], project["id"], "response.completed", "evt_1", {}, max_attempts=2
    )

    for _ in range(2):
        db.record_webhook_attempt(delivery["id"], status="pending", http_status=503)

    assert db.due_webhook_deliveries() == []


def test_no_list_read_of_an_endpoint_carries_the_secret_that_signs_its_deliveries(
    project, tenants
):
    """SCHEMA-V34.md says of `api_webhook_endpoints.secret`: "it is shown once
    in the console and returned by no API". `list_webhook_endpoints` was
    returning it on every row, which is what a console route serialises
    wholesale."""
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook",
        ["response.completed"], "TOP-SECRET",
    )
    db.update_webhook_endpoint(
        endpoint["id"], tenants["a"], secret="rotated", previous_secret="TOP-SECRET",
        previous_secret_expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )

    listed = db.list_webhook_endpoints(project["id"], tenants["a"])[0]
    fetched = db.get_webhook_endpoint(endpoint["id"], tenants["a"])

    for row in (listed, fetched):
        assert "secret" not in row and "previous_secret" not in row
        # The FACT of a secret is not the secret: the console shows that an
        # endpoint is signed and that a rotation is in flight, and no page
        # ever holds the forgery primitive itself.
        assert row["has_secret"] is True and row["has_previous_secret"] is True
        # The date is not a credential either, and the console has to show
        # when the old secret stops being accepted.
        assert row["previous_secret_expires_at"] is not None
        assert row["url"] == "https://example.test/hook"
        assert row["events"] == ["response.completed"]

    # The one caller that genuinely needs the value still has it: the sweep
    # that signs with it.
    db.enqueue_webhook_delivery(endpoint["id"], project["id"], "response.completed", "evt_1", {})
    assert db.due_webhook_deliveries()[0]["endpoint"]["secret"] == "rotated"


def test_an_empty_change_answers_the_current_endpoint_not_a_missing_one(project, tenants):
    """"No changes requested" and "that is not your endpoint" were both None,
    so a route mapping None to 404 would 404 an endpoint that exists and is
    owned by the caller, purely because the PATCH body was empty. The sibling
    `update_api_project` has always fallen through to a scoped read."""
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook",
        ["response.completed"], "shh",
    )

    unchanged = db.update_webhook_endpoint(endpoint["id"], tenants["a"])

    assert unchanged is not None and unchanged["id"] == endpoint["id"]
    assert unchanged["url"] == "https://example.test/hook"
    # The empty PATCH is not a back door to the signing secret: it answers
    # exactly what a scoped read answers.
    assert "secret" not in unchanged
    assert unchanged == db.get_webhook_endpoint(endpoint["id"], tenants["a"])
    # The other tenant, and an id that never existed, still read as missing.
    assert db.update_webhook_endpoint(endpoint["id"], tenants["b"]) is None
    assert db.update_webhook_endpoint("whe_never_issued", tenants["a"]) is None
    # The same call for the project row, so the pair cannot drift apart again.
    assert db.update_api_project(project["id"], tenants["a"]) == db.get_api_project(
        project["id"], tenants["a"]
    )


def test_a_console_retry_may_only_record_an_attempt_on_its_own_delivery(project, tenants):
    """`record_webhook_attempt` is a write addressed by a bare id, which the
    module header's rule 1 says should not exist. The sweep is allowed to call
    it that way — it is the server acting on its own queue — but a "retry this
    delivery" button is a caller naming a row, and it must pass the project
    the session already proved."""
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook",
        ["response.completed"], "shh",
    )
    delivery = db.enqueue_webhook_delivery(
        endpoint["id"], project["id"], "response.completed", "evt_1", {}
    )
    neighbour = db.create_api_project(tenants["b"], "Beta", "live")

    refused = db.record_webhook_attempt(
        delivery["id"], status="delivered", http_status=200, project_id=neighbour["id"]
    )

    assert refused is None
    # Nothing moved: not the attempt counter, not the endpoint's health.
    assert db.due_webhook_deliveries()[0]["attempt"] == 0
    assert db.list_webhook_endpoints(project["id"], tenants["a"])[0]["last_delivery_at"] is None

    allowed = db.record_webhook_attempt(
        delivery["id"], status="delivered", http_status=200, project_id=project["id"]
    )
    assert allowed["attempt"] == 1 and allowed["status"] == "delivered"


# -------------------------------------------------------------- model gates --


def test_public_model_overrides_can_only_narrow_what_the_code_declares(tenants):
    db.set_public_model_enabled(tenants["a"], "techsara-35b", False, updated_by=tenants["owner"])
    db.set_public_model_enabled(tenants["a"], "techsara-router-8b", True)
    db.set_public_model_enabled(tenants["a"], "techsara-vision", True)

    declared = ("techsara-35b", "techsara-vision")
    narrowed = db.public_model_overrides(tenants["a"], declared)

    # The only thing the database can say about a declared model is "off".
    assert narrowed == {"techsara-35b": False}
    # A row for a model the code never declared cannot publish it…
    assert "techsara-router-8b" not in narrowed
    # …though the console still sees every stored row, or it could not fix one.
    assert db.public_model_overrides(tenants["a"]) == {
        "techsara-35b": False, "techsara-router-8b": True, "techsara-vision": True,
    }

    reinstated = db.set_public_model_enabled(tenants["a"], "techsara-35b", True)
    assert reinstated["enabled"] is True
    assert db.public_model_overrides(tenants["a"], declared) == {}


def test_one_workspace_disabling_a_model_leaves_every_other_workspace_serving_it(tenants):
    """The 2026-09-13 finding: `public_models` was keyed by model id alone, so
    the console switch — held by a super admin whose authority is ONE
    workspace — wrote the row every workspace's `/v1/models` read, and one
    tenant's administrator could turn a model off for every customer."""
    declared = ("techsara-35b",)

    db.set_public_model_enabled(tenants["b"], "techsara-35b", False, updated_by=tenants["owner"])

    assert db.public_model_overrides(tenants["b"], declared) == {"techsara-35b": False}
    assert db.public_model_overrides(tenants["a"], declared) == {}
    assert db.public_model_overrides(tenants["a"]) == {}

    # Both workspaces may hold their own row for the same model id.
    db.set_public_model_enabled(tenants["a"], "techsara-35b", True)
    assert db.public_model_overrides(tenants["b"], declared) == {"techsara-35b": False}

    # The old, tenant-less call shapes fail loudly instead of matching nothing.
    with pytest.raises(TypeError):
        db.public_model_overrides(declared)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        db.set_public_model_enabled("techsara-35b", False)  # type: ignore[call-arg]
    with pytest.raises(db.IntegrityError):
        db.set_public_model_enabled("ws-that-does-not-exist", "techsara-35b", False)

    # A workspace's overrides go with the workspace.
    with db.connection() as con:
        con.execute("DELETE FROM workspaces WHERE id = %s", (tenants["b"],))
        left = con.execute("SELECT workspace_id FROM public_models").fetchall()
    assert [row["workspace_id"] for row in left] == [tenants["a"]]

# ---------------------------------------------------------- platform secrets --


def test_a_platform_secret_is_written_once_and_a_second_writer_adopts_the_first(tenants):
    """The pepper's safety property. If a second worker's value overwrote the
    first, every key digest already stored under the first pepper would stop
    verifying, and every customer's key would 401 with nothing in the logs to
    explain it."""
    assert db.get_platform_secret("api_key_pepper") is None

    first = db.set_platform_secret("api_key_pepper", "pepper-from-worker-one")
    second = db.set_platform_secret("api_key_pepper", "pepper-from-worker-two")

    assert first == "pepper-from-worker-one"
    assert second == "pepper-from-worker-one"
    assert db.get_platform_secret("api_key_pepper") == "pepper-from-worker-one"


# ------------------------------------------------------------------ cascades --


def test_deleting_a_project_takes_every_child_row_with_it(project, tenants):
    account = db.create_service_account(project["id"], tenants["a"], "nightly")
    key = _key(project, tenants["a"], service_account_id=account["id"])
    response = db.create_api_response(
        project["id"], tenants["a"], "techsara-35b", "req_1", key_id=key["id"]
    )
    db.claim_idempotency(project["id"], "/v1/responses", "idem-1", "fp")
    db.finish_idempotency(project["id"], "/v1/responses", "idem-1", response["id"])
    db.bump_usage_minute(project["id"], key["id"], requests=1)
    db.bump_usage_daily(project["id"], requests=1)
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook", ["response.completed"], "shh"
    )
    db.enqueue_webhook_delivery(endpoint["id"], project["id"], "response.completed", "evt_1", {})

    with db.connection() as con:
        con.execute("DELETE FROM api_projects WHERE id = %s", (project["id"],))
        remaining = {
            table: con.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in (
                "api_service_accounts", "api_keys", "api_responses", "api_idempotency",
                "api_usage_minute", "api_usage_daily", "api_webhook_endpoints",
                "api_webhook_deliveries",
            )
        }

    assert remaining == dict.fromkeys(remaining, 0)


def test_deleting_a_workspace_takes_its_projects_and_their_keys_with_it(project, tenants):
    key = _key(project, tenants["a"])
    db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1", key_id=key["id"])
    survivor = db.create_api_project(tenants["b"], "Beta", "live")

    with db.connection() as con:
        con.execute("DELETE FROM workspaces WHERE id = %s", (tenants["a"],))

    assert db.get_api_project(project["id"], tenants["a"]) is None
    assert db.api_key_by_public_id(key["public_id"]) is None
    assert [p["id"] for p in db.list_api_projects(tenants["b"])] == [survivor["id"]]


def test_deleting_the_account_that_made_a_key_keeps_the_key_and_forgets_the_author(project, tenants):
    """A leaving employee must not take a live integration's credentials down
    with them; the audit trail of WHO made it lives in audit_events."""
    key = _key(project, tenants["a"], created_by=tenants["owner"])

    with db.connection() as con:
        con.execute("DELETE FROM users WHERE id = %s", (tenants["owner"],))

    still_there = db.api_key_by_public_id(key["public_id"])
    assert still_there is not None and still_there["created_by"] is None


# ----------------------------------------------------------------- retention --


def test_prune_removes_expired_claims_old_counters_and_stale_output(project, tenants):
    key = _key(project, tenants["a"])
    now = datetime.now(timezone.utc)

    keep = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_keep")
    db.update_api_response(keep["id"], project["id"], output_text="recent, still readable")
    expired = db.create_api_response(
        project["id"], tenants["a"], "techsara-35b", "req_gone",
        expires_at=now - timedelta(minutes=1),
    )
    db.claim_idempotency(project["id"], "/v1/responses", "idem-old", "fp", ttl_hours=-1)
    db.claim_idempotency(project["id"], "/v1/responses", "idem-live", "fp")
    db.bump_usage_minute(project["id"], key["id"], requests=1, at=now - timedelta(days=30))
    db.bump_usage_minute(project["id"], key["id"], requests=1, at=now)

    removed = db.prune_api_platform(now=now)

    assert removed["api_idempotency"] == 1
    assert removed["api_usage_minute"] == 1
    assert removed["api_responses"] == 1
    assert db.get_api_response(expired["id"], project["id"]) is None
    assert db.get_idempotency(project["id"], "/v1/responses", "idem-old") is None
    assert db.get_idempotency(project["id"], "/v1/responses", "idem-live") is not None
    assert db.get_api_response(keep["id"], project["id"])["output_text"] == "recent, still readable"

    # Shorten the project's retention and the stored text goes, while the row
    # that the usage record refers to stays.
    db.update_api_project(project["id"], tenants["a"], retention_days=0)
    cleared = db.prune_api_platform(now=now + timedelta(seconds=1))

    assert cleared["api_responses_output_text"] == 1
    assert db.get_api_response(keep["id"], project["id"])["output_text"] is None


# ------------------------------------------------- wave-3 residuals, 2026-09-13 --
#
# Each test below closes one finding of the adversarial review of 2026-09-13
# and was watched failing against the code it replaced before the fix landed.


def _assert_no_secret_material(row, *, where: str) -> None:
    for column in ("key_hash", "secret", "previous_secret"):
        assert column not in row, f"{where} returned {column}"


def test_no_row_any_key_or_endpoint_accessor_returns_carries_a_digest_or_a_signing_secret(
    project, tenants
):
    """`create_api_key` and `update_webhook_endpoint` were `RETURNING *`: the
    first handed back the HMAC digest a key is verified against, the second
    handed back the live signing secret — and the previous one — on a PATCH of
    nothing more than `status`. Every accessor that returns a key or endpoint
    row to a request path is called here, every mutation shape included, and
    none may carry secret material. The two readers that genuinely need it
    (`api_key_by_public_id`, `due_webhook_deliveries`) are the only exceptions
    and are asserted as such."""
    now = datetime.now(timezone.utc)
    key = _key(project, tenants["a"], public_id="pub0000000000001")
    successor = _key(
        project, tenants["a"], public_id="pub0000000000002", rotated_from=key["public_id"]
    )
    key_rows = {
        "create_api_key": key,
        "create_api_key(rotated_from)": successor,
        "list_api_keys": db.list_api_keys(project["id"], tenants["a"])[0],
        "update_api_key_rotation": db.update_api_key_rotation(key["id"], tenants["a"], now),
        "expire_rotated_keys": db.expire_rotated_keys(now=now)[0],
        "revoke_api_key": db.revoke_api_key(successor["id"], tenants["a"]),
    }
    for where, row in key_rows.items():
        _assert_no_secret_material(row, where=where)
        assert row["public_id"] and row["last_four"] == "ab12"

    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook",
        ["response.completed"], "SIGNING-SECRET",
    )
    endpoint_rows = {
        "create_webhook_endpoint": endpoint,
        "update(status)": db.update_webhook_endpoint(endpoint["id"], tenants["a"], status="disabled"),
        "update(events)": db.update_webhook_endpoint(
            endpoint["id"], tenants["a"], events=["response.failed"]
        ),
        "update(url)": db.update_webhook_endpoint(
            endpoint["id"], tenants["a"], url="https://example.test/other"
        ),
        "update(include_output)": db.update_webhook_endpoint(
            endpoint["id"], tenants["a"], include_output=True
        ),
        "update(rotation)": db.update_webhook_endpoint(
            endpoint["id"], tenants["a"], status="active", secret="NEXT-SECRET",
            previous_secret="SIGNING-SECRET",
            previous_secret_expires_at=now + timedelta(days=1),
        ),
        "update()": db.update_webhook_endpoint(endpoint["id"], tenants["a"]),
        "get_webhook_endpoint": db.get_webhook_endpoint(endpoint["id"], tenants["a"]),
        "list_webhook_endpoints": db.list_webhook_endpoints(project["id"], tenants["a"])[0],
    }
    for where, row in endpoint_rows.items():
        _assert_no_secret_material(row, where=where)
        assert row["has_secret"] is True, where
        assert "SIGNING-SECRET" not in repr(row) and "NEXT-SECRET" not in repr(row), where

    assert db.api_key_by_public_id(key["public_id"])["key_hash"]
    db.enqueue_webhook_delivery(endpoint["id"], project["id"], "response.completed", "evt_1", {})
    assert db.due_webhook_deliveries()[0]["endpoint"]["secret"] == "NEXT-SECRET"


def test_a_webhook_patch_cannot_move_an_endpoint_to_a_plaintext_url(project, tenants):
    """A check that guards only the POST is walked around by the PATCH; the
    create-side test was the only one, and deleting the update-side check
    left the suite green."""
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook", ["response.completed"], "shh"
    )
    for url in ("http://example.test/hook", "HTTP://example.test/hook", "ftp://example.test"):
        with pytest.raises(ValueError):
            db.update_webhook_endpoint(endpoint["id"], tenants["a"], url=url)
    assert db.get_webhook_endpoint(endpoint["id"], tenants["a"])["url"] == "https://example.test/hook"


# idempotency expiry


def _expire_claim(project_id: str, endpoint: str, idem_key: str) -> None:
    with db.connection() as con:
        con.execute(
            "UPDATE api_idempotency SET expires_at = now() - interval '1 second' "
            "WHERE project_id = %s AND endpoint = %s AND idem_key = %s",
            (project_id, endpoint, idem_key),
        )


def test_an_expired_idempotency_claim_reads_as_absent_and_can_be_claimed_again(project, tenants):
    """A request that crashed between claim and finish pinned its key at 429
    "still running" forever: `get_idempotency` ignored `expires_at`, the claim
    was DO NOTHING against any existing row, and nothing scheduled the prune.
    CONTRACT §13 says 24 hours."""
    crashed = db.claim_idempotency(project["id"], "/v1/responses", "idem-crash", "fp-old")
    assert crashed is not None
    _expire_claim(project["id"], "/v1/responses", "idem-crash")

    assert db.get_idempotency(project["id"], "/v1/responses", "idem-crash") is None

    retried = db.claim_idempotency(project["id"], "/v1/responses", "idem-crash", "fp-new")

    assert retried is not None and retried["state"] == "in_flight"
    assert retried["fingerprint"] == "fp-new" and retried["response_id"] is None
    assert retried["id"] != crashed["id"]
    live = db.get_idempotency(project["id"], "/v1/responses", "idem-crash")
    assert live["id"] == retried["id"]
    # The renewed claim is a real one: a third caller loses to it.
    assert db.claim_idempotency(project["id"], "/v1/responses", "idem-crash", "fp-new") is None

    # A COMPLETED claim expires the same way: the replay promise is 24 h, not forever.
    response = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1")
    db.claim_idempotency(project["id"], "/v1/responses", "idem-done", "fp")
    db.finish_idempotency(project["id"], "/v1/responses", "idem-done", response["id"])
    _expire_claim(project["id"], "/v1/responses", "idem-done")
    assert db.get_idempotency(project["id"], "/v1/responses", "idem-done") is None
    assert db.claim_idempotency(project["id"], "/v1/responses", "idem-done", "fp") is not None


def test_a_negative_ttl_claim_is_born_expired_and_never_blocks_a_retry(project, tenants):
    """The verifier's reproduction, verbatim: `ttl_hours=-48` was still
    returned by `get_idempotency`."""
    db.claim_idempotency(project["id"], "/v1/responses", "idem-neg", "fp", ttl_hours=-48)

    assert db.get_idempotency(project["id"], "/v1/responses", "idem-neg") is None
    assert db.claim_idempotency(project["id"], "/v1/responses", "idem-neg", "fp") is not None


def test_twelve_concurrent_retries_of_an_expired_claim_produce_exactly_one_new_holder(
    project, tenants
):
    """The takeover must be as race-free as the original claim. Twelve real
    threads on twelve pooled connections, released by one barrier, all see
    the same expired row; ON CONFLICT DO UPDATE ... WHERE expires_at <= now()
    locks it, and every claimant that queued behind the winner re-checks the
    WHERE against the row the winner just renewed."""
    db.claim_idempotency(project["id"], "/v1/responses", "idem-race", "fp")
    _expire_claim(project["id"], "/v1/responses", "idem-race")
    ready = threading.Barrier(12)
    won: list = []
    errors: list = []

    def retry() -> None:
        try:
            ready.wait(timeout=10)
            claim = db.claim_idempotency(project["id"], "/v1/responses", "idem-race", "fp")
            if claim is not None:
                won.append(claim["id"])
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=retry) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert len(won) == 1
    assert db.get_idempotency(project["id"], "/v1/responses", "idem-race")["id"] == won[0]


def test_a_late_request_cannot_finish_or_release_a_claim_that_was_taken_over(project, tenants):
    response = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1")
    stale = db.claim_idempotency(project["id"], "/v1/responses", "idem-late", "fp")
    _expire_claim(project["id"], "/v1/responses", "idem-late")

    # Finishing an expired claim is refused even without a claim id.
    assert db.finish_idempotency(project["id"], "/v1/responses", "idem-late", response["id"]) is None

    fresh = db.claim_idempotency(project["id"], "/v1/responses", "idem-late", "fp")
    # The crashed request wakes up and tries to finish or release by its own id.
    assert db.finish_idempotency(
        project["id"], "/v1/responses", "idem-late", response["id"], claim_id=stale["id"]
    ) is None
    assert db.release_idempotency(
        project["id"], "/v1/responses", "idem-late", claim_id=stale["id"]
    ) is False
    assert db.get_idempotency(project["id"], "/v1/responses", "idem-late")["state"] == "in_flight"

    done = db.finish_idempotency(
        project["id"], "/v1/responses", "idem-late", response["id"], claim_id=fresh["id"]
    )
    assert done["state"] == "completed"


def test_a_failed_request_releases_its_claim_but_a_completed_one_is_never_released(
    project, tenants
):
    neighbour = db.create_api_project(tenants["b"], "Beta", "live")
    claim = db.claim_idempotency(project["id"], "/v1/responses", "idem-fail", "fp")

    assert db.release_idempotency(neighbour["id"], "/v1/responses", "idem-fail") is False
    assert db.release_idempotency(
        project["id"], "/v1/responses", "idem-fail", claim_id=claim["id"]
    ) is True
    assert db.get_idempotency(project["id"], "/v1/responses", "idem-fail") is None
    assert db.claim_idempotency(project["id"], "/v1/responses", "idem-fail", "fp") is not None

    response = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_1")
    db.finish_idempotency(project["id"], "/v1/responses", "idem-fail", response["id"])
    # Releasing a completed claim would let the same key invoke the model twice.
    assert db.release_idempotency(project["id"], "/v1/responses", "idem-fail") is False
    assert db.get_idempotency(project["id"], "/v1/responses", "idem-fail")["state"] == "completed"


# usage ledgers


def test_a_usage_increment_may_not_be_negative_so_a_quota_cannot_be_refunded(project, tenants):
    """Measured 2026-09-13: `bump_usage_daily(requests=-100,
    input_tokens=-1_000_000)` left the day at -90 requests. The accessors
    refuse the increment (a CHECK on the running total cannot see a partial
    refund); the CHECK is the backstop for every other writer."""
    key = _key(project, tenants["a"])
    db.bump_usage_daily(project["id"], requests=10, input_tokens=1000)
    db.bump_usage_minute(project["id"], key["id"], requests=10, input_tokens=1000)

    for column in ("requests", "input_tokens", "output_tokens", "errors", "rate_limited"):
        with pytest.raises(ValueError):
            db.bump_usage_daily(project["id"], **{column: -1})
    for column in ("requests", "input_tokens", "output_tokens"):
        with pytest.raises(ValueError):
            db.bump_usage_minute(project["id"], key["id"], **{column: -1})
    # A partial refund that would leave the total positive is refused too.
    with pytest.raises(ValueError):
        db.bump_usage_daily(project["id"], requests=-5)

    window = db.usage_window(project["id"], key["id"])
    assert window["daily"]["requests"] == 10 and window["daily"]["input_tokens"] == 1000
    assert window["minute"]["requests"] == 10

    for statement in (
        "UPDATE api_usage_daily SET requests = -1 WHERE project_id = %s",
        "UPDATE api_usage_daily SET input_tokens = -1 WHERE project_id = %s",
        "UPDATE api_usage_daily SET output_tokens = -1 WHERE project_id = %s",
        "UPDATE api_usage_daily SET errors = -1 WHERE project_id = %s",
        "UPDATE api_usage_daily SET rate_limited = -1 WHERE project_id = %s",
        "UPDATE api_usage_minute SET requests = -1 WHERE project_id = %s",
        "UPDATE api_usage_minute SET input_tokens = -1 WHERE project_id = %s",
        "UPDATE api_usage_minute SET output_tokens = -1 WHERE project_id = %s",
    ):
        with pytest.raises(db.IntegrityError):
            with db.connection() as con:
                con.execute(statement, (project["id"],))


def test_another_projects_daily_spend_is_neither_charged_nor_refunded_to_this_one(
    project, tenants
):
    """A mutation that deleted `project_id = %s` from the daily read in
    `usage_window` survived the whole suite, because no test ever put daily
    usage in two projects. Same for `read_usage_daily`."""
    key = _key(project, tenants["a"])
    other = db.create_api_project(tenants["b"], "Beta", "live")
    today = datetime.now(timezone.utc)

    db.bump_usage_daily(project["id"], requests=3, input_tokens=30, day=today)
    db.bump_usage_daily(other["id"], requests=900, input_tokens=900_000, errors=7, day=today)

    daily = db.usage_window(project["id"], key["id"], at=today)["daily"]
    assert daily == {
        "requests": 3, "input_tokens": 30, "output_tokens": 0, "errors": 0, "rate_limited": 0,
    }
    rows = db.read_usage_daily(project["id"], today.date(), today.date())
    assert [(row["project_id"], row["requests"]) for row in rows] == [(project["id"], 3)]


def test_the_window_reports_project_minute_totals_summed_across_every_key(project, tenants):
    """Limits are per PROJECT, and a key may only tighten its share. On
    2026-09-13 ten keys in one rpm=60 project admitted 600 requests in a
    minute, because the only minute counters the quota gate could read were
    the calling key's. The project totals come from the same statement."""
    first = _key(project, tenants["a"], public_id="pub0000000000001")
    second = _key(project, tenants["a"], public_id="pub0000000000002")
    stranger_project = db.create_api_project(tenants["b"], "Beta", "live")
    stranger = _key(stranger_project, tenants["b"], public_id="pub0000000000003")
    now = datetime.now(timezone.utc).replace(second=30, microsecond=0)

    db.bump_usage_minute(project["id"], first["id"], requests=4, input_tokens=40, at=now)
    db.bump_usage_minute(project["id"], second["id"], requests=6, output_tokens=60, at=now)
    db.bump_usage_minute(
        project["id"], second["id"], requests=5, at=now - timedelta(minutes=1)
    )
    db.bump_usage_minute(stranger_project["id"], stranger["id"], requests=1000, at=now)

    window = db.usage_window(project["id"], first["id"], at=now)

    assert window["minute"] == {"requests": 4, "input_tokens": 40, "output_tokens": 0}
    assert window["previous"] == {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    assert window["project_minute"] == {"requests": 10, "input_tokens": 40, "output_tokens": 60}
    assert window["project_previous"] == {"requests": 5, "input_tokens": 0, "output_tokens": 0}


def test_twelve_concurrent_requests_through_an_rpm_1_project_admit_exactly_one_under_the_lock(
    project, tenants
):
    """The shape `quotas.reserve` is built on, proved against real PostgreSQL
    the way the bug was found: 12 simultaneous requests, rpm=1, 12 admitted.

    Each thread opens ONE transaction, takes `lock_api_project_usage`, reads
    the project window, decides, and bumps — every accessor given the same
    `con`. The sleep between read and write is deliberate: it holds the
    read-decide-write window open long enough that, without the advisory
    lock, every thread reads zero and all twelve are admitted (which is what
    this test reports if the lock line is removed). Two keys of the same
    project are used so the limit is visibly per project, not per key.
    """
    import time

    keys = [
        _key(project, tenants["a"], public_id="pub0000000000001"),
        _key(project, tenants["a"], public_id="pub0000000000002"),
    ]
    rpm = 1
    at = datetime.now(timezone.utc)
    ready = threading.Barrier(12)
    admitted: list = []
    refused: list = []
    errors: list = []

    def request(index: int) -> None:
        key = keys[index % 2]
        try:
            ready.wait(timeout=10)
            with db.connection() as con:
                db.lock_api_project_usage(con, project["id"])
                window = db.usage_window(project["id"], key["id"], at=at, con=con)
                if window["project_minute"]["requests"] + 1 > rpm:
                    refused.append(index)
                    return
                time.sleep(0.05)
                db.bump_usage_minute(project["id"], key["id"], requests=1, at=at, con=con)
                db.bump_usage_daily(project["id"], requests=1, day=at, con=con)
                admitted.append(index)
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=request, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert (len(admitted), len(refused)) == (1, 11)
    window = db.usage_window(project["id"], keys[0]["id"], at=at)
    assert window["project_minute"]["requests"] == 1
    assert window["daily"]["requests"] == 1


def test_a_usage_bump_inside_a_rolled_back_transaction_is_not_counted(project, tenants):
    """`con` really is the caller's transaction, not a side channel that
    commits on its own: a reservation that fails later must leave no spend."""
    key = _key(project, tenants["a"])

    with pytest.raises(RuntimeError):
        with db.connection() as con:
            db.bump_usage_minute(project["id"], key["id"], requests=1, con=con)
            db.bump_usage_daily(project["id"], requests=1, con=con)
            raise RuntimeError("the engine refused the request")

    window = db.usage_window(project["id"], key["id"])
    assert window["minute"]["requests"] == 0 and window["daily"]["requests"] == 0


# allow-list shape and size


def test_a_mixed_type_set_is_a_value_error_not_a_type_error(project, tenants):
    """`sorted({"a", 1})` ran before the item check and raised TypeError, which
    a route mapping ValueError to 400 answers as a 500."""
    with pytest.raises(ValueError):
        db.create_api_project(tenants["a"], "Mixed", "live", allowed_models={"a", 1})
    with pytest.raises(ValueError):
        db.update_api_project(project["id"], tenants["a"], ip_allowlist=frozenset({"10.0.0.0/8", 3}))


def test_an_allowlist_is_bounded_in_entries_and_in_entry_length(project, tenants):
    """5,000 entries of 1,000 characters were accepted into `allowed_models`,
    and the resolver reads the project row on every `/v1` request."""
    with pytest.raises(ValueError):
        db.update_api_project(project["id"], tenants["a"], allowed_models=["m"] * 257)
    with pytest.raises(ValueError):
        db.update_api_project(project["id"], tenants["a"], allowed_origins=["x" * 513])
    with pytest.raises(ValueError):
        _key(project, tenants["a"], scopes=[f"s{i}" for i in range(257)])

    at_the_bound = db.update_api_project(
        project["id"], tenants["a"], allowed_models=[f"m{i}" for i in range(256)]
    )
    assert len(at_the_bound["allowed_models"]) == 256

    # The entry count is the schema's rule too, for every writer.
    with pytest.raises(db.IntegrityError):
        with db.connection() as con:
            con.execute(
                "UPDATE api_projects SET allowed_models = "
                "(SELECT jsonb_agg(i::text) FROM generate_series(1, 257) i) WHERE id = %s",
                (project["id"],),
            )
    # …and a scalar is still a constraint violation, not a function error.
    with pytest.raises(db.IntegrityError):
        with db.connection() as con:
            con.execute(
                "UPDATE api_projects SET ip_allowlist = '\"10.0.0.1\"'::jsonb WHERE id = %s",
                (project["id"],),
            )


# resolver snapshot


class _InterleavedConnection:
    """A pooled connection that lets another session commit a change right
    after the resolver's FIRST read — the one moment the three-read
    transaction used to be exposed at."""

    def __init__(self, con, after_first_read):
        self._con = con
        self._after_first_read = after_first_read
        self._reads = 0

    def execute(self, statement, params=None):
        cursor = self._con.execute(statement, params)
        if str(statement).lstrip().upper().startswith("SELECT"):
            self._reads += 1
            if self._reads == 1:
                self._after_first_read()
        return cursor


def test_the_resolver_read_sees_the_key_its_project_and_its_account_at_one_instant(
    project, tenants, monkeypatch
):
    """`api_key_by_public_id` promised its reads share one snapshot while the
    pool ran READ COMMITTED, where each statement takes a fresh one. Here a
    second session disables the project and the service account between the
    key read and the project read: under REPEATABLE READ the principal is the
    state of one instant (all active), and the very next resolution sees the
    change — nothing is cached."""
    import contextlib

    account = db.create_service_account(project["id"], tenants["a"], "nightly")
    key = _key(project, tenants["a"], service_account_id=account["id"])
    real_connection = db.connection

    def disable_both() -> None:
        with psycopg.connect(db.dsn(), autocommit=True) as other:
            other.execute(
                "UPDATE api_projects SET status = 'disabled' WHERE id = %s", (project["id"],)
            )
            other.execute(
                "UPDATE api_service_accounts SET status = 'disabled' WHERE id = %s",
                (account["id"],),
            )

    @contextlib.contextmanager
    def interleaved():
        with real_connection() as con:
            yield _InterleavedConnection(con, disable_both)

    monkeypatch.setattr(db, "connection", interleaved)
    during = db.api_key_by_public_id(key["public_id"])
    monkeypatch.setattr(db, "connection", real_connection)

    assert during["status"] == "active"
    assert during["project"]["status"] == "active"
    assert during["service_account"]["status"] == "active"

    after = db.api_key_by_public_id(key["public_id"])
    assert after["project"]["status"] == "disabled"
    assert after["service_account"]["status"] == "disabled"


# retention


def test_prune_never_deletes_a_pending_delivery_however_old(project, tenants):
    """A mutation adding 'pending' to prune's delivery statuses survived the
    suite: the deliveries count was never asserted. A pending delivery is a
    promise not yet kept, and pruning it is the silent drop §14 forbids."""
    endpoint = db.create_webhook_endpoint(
        project["id"], tenants["a"], "https://example.test/hook", ["response.completed"], "shh"
    )
    pending = db.enqueue_webhook_delivery(
        endpoint["id"], project["id"], "response.completed", "evt_pending", {}
    )
    finished = db.enqueue_webhook_delivery(
        endpoint["id"], project["id"], "response.completed", "evt_done", {}
    )
    db.record_webhook_attempt(finished["id"], status="delivered", http_status=200)
    with db.connection() as con:
        con.execute(
            "UPDATE api_webhook_deliveries SET created_at = now() - interval '90 days'"
        )

    removed = db.prune_api_platform()

    assert removed["api_webhook_deliveries"] == 1
    with db.connection() as con:
        left = [
            row["id"] for row in con.execute("SELECT id FROM api_webhook_deliveries").fetchall()
        ]
    assert left == [pending["id"]]


def test_prune_works_through_a_large_backlog_in_committed_batches(project, tenants):
    """Retention ran as five unbounded statements in one transaction under a
    15 s statement timeout, so a backlog big enough to need pruning rolled
    everything back. Each job now commits batch by batch; with a batch of 2
    and 7 expired claims the job needs four batches and removes all seven."""
    for index in range(7):
        db.claim_idempotency(project["id"], "/v1/responses", f"old-{index}", "fp", ttl_hours=-1)
    db.claim_idempotency(project["id"], "/v1/responses", "live", "fp")

    removed = db.prune_api_platform(batch_size=2)

    assert removed["api_idempotency"] == 7
    with db.connection() as con:
        keys = [row["idem_key"] for row in con.execute("SELECT idem_key FROM api_idempotency").fetchall()]
    assert keys == ["live"]

    # A bounded run stops at `max_batches` and the next run continues.
    for index in range(5):
        db.claim_idempotency(project["id"], "/v1/responses", f"again-{index}", "fp", ttl_hours=-1)
    assert db.prune_api_platform(batch_size=2, max_batches=1)["api_idempotency"] == 2
    assert db.prune_api_platform(batch_size=2)["api_idempotency"] == 3



# ------------------------------------------------- wave-4 residuals, 2026-09-13 --
#
# The re-verifier of the wave-3 fixes found that they closed their targets and
# opened new holes: two clocks on the idempotency path, two lock keys for one
# project, a playground row a customer's project name could block, and a
# rotation sweep nobody ran. Each test below was watched failing against the
# code it replaces (or, for the pool tests, against the fix switched off).


def _db_now() -> datetime:
    with db.connection() as con:
        return con.execute("SELECT now() AS t").fetchone()["t"]


def test_a_claim_the_database_declined_is_visible_to_the_read_that_follows_it(project, tenants):
    """reverify-database, wave 3: the claim decided "not expired" on one clock
    and the read decided "expired" on another, so a row expiring between them
    was neither taken over nor visible and the request answered 500.

    Reproduced deterministically with PostgreSQL's own transaction clock: the
    claim runs in a transaction that started BEFORE the row expired, the row
    expires in real time, and then the read runs. Two autocommitted statements
    (the old shape) lose the row; `claim_or_get_idempotency` on one transaction
    cannot, because its claim and its read see the same `now()`."""
    import time

    endpoint, key = "/v1/responses", "idem-between"
    assert db.claim_idempotency(project["id"], endpoint, key, "fp") is not None
    with db.connection() as con:
        con.execute(
            "UPDATE api_idempotency SET expires_at = clock_timestamp() + interval '400 milliseconds' "
            "WHERE project_id = %s AND idem_key = %s",
            (project["id"], key),
        )

    # The OLD shape: claim on a clock taken before expiry, read after it.
    with db.connection() as claimer:
        claimer.execute("SELECT now()")  # fixes this transaction's clock
        time.sleep(0.6)
        assert db.claim_idempotency(project["id"], endpoint, key, "fp", con=claimer) is None
    assert db.get_idempotency(project["id"], endpoint, key) is None  # the gap, on two clocks

    # Restore a row that expires in the future relative to a fresh transaction.
    with db.connection() as con:
        con.execute(
            "UPDATE api_idempotency SET expires_at = clock_timestamp() + interval '400 milliseconds' "
            "WHERE project_id = %s AND idem_key = %s",
            (project["id"], key),
        )
    with db.connection() as con:
        con.execute("SELECT now()")
        time.sleep(0.6)
        claimed, row = db.claim_or_get_idempotency(project["id"], endpoint, key, "fp", con=con)
    assert claimed is False and row is not None and row["fingerprint"] == "fp"

    # And on its own connection an expired row is simply taken over — one
    # clock, so there is no third outcome.
    claimed, row = db.claim_or_get_idempotency(project["id"], endpoint, key, "fp")
    assert claimed is True and row["state"] == "in_flight"


def test_claim_or_get_returns_the_claim_id_that_pins_finish_to_the_winner(project, tenants):
    """The id it returns is the `claim_id` finish/release take, so a request
    whose claim was taken over cannot stamp its answer on the new holder."""
    endpoint = "/v1/responses"
    won, first = db.claim_or_get_idempotency(project["id"], endpoint, "idem-pin", "fp")
    lost, seen = db.claim_or_get_idempotency(project["id"], endpoint, "idem-pin", "fp")
    assert (won, lost) == (True, False) and seen["id"] == first["id"]

    _expire_claim(project["id"], endpoint, "idem-pin")
    retaken, second = db.claim_or_get_idempotency(project["id"], endpoint, "idem-pin", "fp")
    assert retaken is True and second["id"] != first["id"]
    zombie = db.create_api_response(project["id"], tenants["a"], "techsara-35b", "req_zombie")
    assert db.finish_idempotency(
        project["id"], endpoint, "idem-pin", zombie["id"], claim_id=first["id"]
    ) is None
    assert db.get_idempotency(project["id"], endpoint, "idem-pin")["state"] == "in_flight"


def test_a_lease_and_a_failed_original_are_taken_over_only_for_the_same_body(project, tenants):
    """The request path's two extra takeovers, now expressible on the database
    clock: an in-flight claim older than the lease, and a completed claim with
    no response. A different body is a 409 case and is never taken over."""
    endpoint = "/v1/responses"
    db.claim_idempotency(project["id"], endpoint, "idem-lease", "fp")
    with db.connection() as con:
        con.execute(
            "UPDATE api_idempotency SET created_at = now() - interval '10 minutes' "
            "WHERE idem_key = 'idem-lease'"
        )
    assert db.claim_idempotency(project["id"], endpoint, "idem-lease", "fp") is None
    assert db.claim_idempotency(
        project["id"], endpoint, "idem-lease", "other", lease_seconds=60
    ) is None
    assert db.claim_idempotency(
        project["id"], endpoint, "idem-lease", "fp", lease_seconds=3600
    ) is None
    assert db.claim_idempotency(
        project["id"], endpoint, "idem-lease", "fp", lease_seconds=60
    ) is not None

    db.claim_idempotency(project["id"], endpoint, "idem-failed", "fp")
    db.finish_idempotency(project["id"], endpoint, "idem-failed", None)
    assert db.claim_idempotency(project["id"], endpoint, "idem-failed", "fp") is None
    assert db.claim_idempotency(
        project["id"], endpoint, "idem-failed", "other", reclaim_failed=True
    ) is None

    ready = threading.Barrier(12)
    winners: list = []
    errors: list = []

    def retry() -> None:
        try:
            ready.wait(timeout=10)
            claimed, _row = db.claim_or_get_idempotency(
                project["id"], endpoint, "idem-failed", "fp", reclaim_failed=True
            )
            if claimed:
                winners.append(1)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=retry, daemon=True) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == [] and len(winners) == 1


def test_both_spellings_of_the_project_usage_lock_take_the_one_documented_key(project, tenants):
    """reverify-database, wave 3: `quotas.reserve` locked a two-int4 key and
    this module a bigint one, so the two never excluded each other. There is
    one key form now, `API_PROJECT_USAGE_LOCK_SQL`, and a lock taken through
    either spelling blocks exactly that key from ANOTHER session (a second
    process, not the pool)."""
    probe_sql = (
        "SELECT pg_try_advisory_xact_lock(hashtextextended('api_project_usage:' || %s, 0)) AS ok"
    )
    assert "hashtextextended('api_project_usage:' || %s, 0)" in db.API_PROJECT_USAGE_LOCK_SQL

    for spelling in ("positional", "keyword", "legacy", "transaction"):
        with psycopg.connect(db.dsn(), autocommit=True, connect_timeout=5) as other:
            if spelling == "transaction":
                with db.api_project_usage_transaction(project["id"]):
                    assert other.execute(probe_sql, (project["id"],)).fetchone()[0] is False
                    assert other.execute(probe_sql, ("proj_elsewhere",)).fetchone()[0] is True
            else:
                with db.connection() as con:
                    if spelling == "positional":
                        db.lock_api_project_usage(project["id"], con=con)
                    elif spelling == "keyword":
                        db.lock_api_project_usage(project_id=project["id"], con=con)
                    else:
                        db.lock_api_project_usage(con, project["id"])
                    assert other.execute(probe_sql, (project["id"],)).fetchone()[0] is False
            # Released by the commit, with no unlock to forget.
            assert other.execute(probe_sql, (project["id"],)).fetchone()[0] is True

    # A transaction-scoped lock on a connection of its own would be gone
    # before the caller did anything under it, so there is no such call.
    with pytest.raises(TypeError):
        db.lock_api_project_usage(project["id"])
    with pytest.raises(TypeError):
        db.lock_api_project_usage(project_id=project["id"])


def test_forty_requests_for_one_project_park_one_pooled_connection_not_all_of_them(
    project, tenants
):
    """THE POOL RULE. The app has ONE psycopg pool (16) shared with chat. A
    request that checks a connection out and THEN waits for the advisory lock
    parks it; with more waiters than the pool, every connection is parked and
    the whole app queues behind one busy project — and a holder that needs a
    second connection never gets one (reverify-console-api, wave 3: 9 of 24
    PoolTimeouts after 10 s).

    `api_project_usage_transaction` waits on a process-local lock BEFORE the
    checkout. 40 threads (more than the pool) each hold the project for 100 ms
    and, inside, deliberately reach for a SECOND pooled connection; a victim
    thread meanwhile measures an unrelated `SELECT 1`. With the process lock
    removed this test fails in seconds with PoolTimeout errors and a victim
    that waited behind the queue; the pool timeout is cut to 3 s and every
    join is bounded, so a regression fails fast instead of hanging CI."""
    import time

    workers = 40
    pool = db.pool()
    assert pool.max_size < workers
    previous_timeout = pool.timeout
    pool.timeout = 3.0
    key = _key(project, tenants["a"])
    at = datetime.now(timezone.utc)
    ready = threading.Barrier(workers + 1)
    done = threading.Event()
    admitted: list = []
    errors: list = []
    victim_waits: list = []

    def request() -> None:
        try:
            ready.wait(timeout=10)
            with db.api_project_usage_transaction(project["id"]) as con:
                window = db.usage_window(project["id"], key["id"], at=at, con=con)
                time.sleep(0.1)
                # The forbidden nested checkout: survivable only because no
                # other thread of this process is parked holding a connection.
                assert db.get_api_project(project["id"], tenants["a"]) is not None
                if window["project_minute"]["requests"] < 1:
                    db.bump_usage_minute(project["id"], key["id"], requests=1, at=at, con=con)
                    admitted.append(1)
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            errors.append(exc)

    def victim() -> None:
        ready.wait(timeout=10)
        time.sleep(0.3)  # let the flood take its connections first
        while not done.is_set():
            started = time.monotonic()
            try:
                with db.connection() as con:
                    con.execute("SELECT 1")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return
            victim_waits.append(time.monotonic() - started)
            time.sleep(0.05)

    try:
        threads = [threading.Thread(target=request, daemon=True) for _ in range(workers)]
        watcher = threading.Thread(target=victim, daemon=True)
        watcher.start()
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 60
        for thread in threads:
            thread.join(timeout=max(0.1, deadline - time.monotonic()))
        done.set()
        watcher.join(timeout=10)
    finally:
        pool.timeout = previous_timeout

    assert not any(thread.is_alive() for thread in threads), "requests hung on the pool"
    assert errors == []
    assert len(admitted) == 1
    assert victim_waits and max(victim_waits) < 1.0, max(victim_waits or [0])
    # The per-project registry forgets a project nobody is using.
    assert project["id"] not in db._usage_locks


def test_the_process_lock_gives_up_on_time_and_never_blocks_another_project(project, tenants):
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with db.api_project_usage_process_lock(project["id"]):
            held.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(TimeoutError):
            with db.api_project_usage_process_lock(project["id"], timeout=0.05):
                pass
        with db.api_project_usage_process_lock("proj_unrelated", timeout=0.05):
            pass
    finally:
        release.set()
        thread.join(timeout=10)
    assert db._usage_locks == {}


def test_a_customer_project_name_cannot_block_the_workspace_playground(tenants):
    """reverify-console-api, wave 3: the playground row had to take a name
    under the per-workspace unique index, and an admin who created both names
    it tried made every playground run a 500. The row is marked by
    `is_playground`, outside the name index, one per workspace."""
    db.create_api_project(tenants["a"], "Console playground", "live")

    playground = db.create_api_project(
        tenants["a"], "Console playground", "live",
        project_id="proj_playground_x", is_playground=True,
    )
    assert playground["is_playground"] is True
    # Customer projects still collide with each other by name.
    with pytest.raises(db.IntegrityError):
        db.create_api_project(tenants["a"], "console playground", "test")
    # Exactly one playground row per workspace, raced on the index.
    with pytest.raises(db.IntegrityError):
        db.create_api_project(
            tenants["a"], "Another", "live", project_id="proj_playground_y", is_playground=True
        )
    other = db.create_api_project(
        tenants["b"], "Console playground", "live",
        project_id="proj_playground_b", is_playground=True,
    )

    assert db.get_playground_project(tenants["a"])["id"] == playground["id"]
    assert db.get_playground_project(tenants["b"])["id"] == other["id"]
    listed = {row["id"] for row in db.list_api_projects(tenants["a"], include_playground=False)}
    assert playground["id"] not in listed and len(listed) == 1
    assert playground["id"] in {row["id"] for row in db.list_api_projects(tenants["a"])}
    # No request body can set or clear the mark, and a plain create never sets it.
    with pytest.raises(ValueError):
        db.update_api_project(playground["id"], tenants["a"], is_playground=False)
    assert db.create_api_project(tenants["a"], "Plain", "live")["is_playground"] is False


def test_the_scheduled_prune_revokes_a_rotated_out_key_past_its_deadline(project, tenants):
    """reverify-database, wave 3: `expire_rotated_keys` had no caller, so a key
    past its rotation deadline was refused by the resolver and listed as
    active forever. `prune_api_platform` is what main.py schedules."""
    now = datetime.now(timezone.utc)
    old = _key(project, tenants["a"], public_id="pub0000000000001", name="old")
    live = _key(project, tenants["a"], public_id="pub0000000000002", name="new")
    db.update_api_key_rotation(old["id"], tenants["a"], now - timedelta(seconds=1))

    removed = db.prune_api_platform()

    assert removed["api_keys_rotated_out"] == 1
    assert [row["id"] for row in db.list_api_keys(project["id"], tenants["a"], include_revoked=False)] == [live["id"]]
    assert db.prune_api_platform()["api_keys_rotated_out"] == 0


def test_the_prune_decides_an_idempotency_claim_is_expired_on_the_database_clock(project, tenants):
    """A Python clock ahead of the database's used to delete a claim that the
    claim and the read both still considered live."""
    db.claim_idempotency(project["id"], "/v1/responses", "idem-clock", "fp")
    with db.connection() as con:
        con.execute(
            "UPDATE api_idempotency SET expires_at = now() + interval '30 seconds' "
            "WHERE idem_key = 'idem-clock'"
        )
    ahead = _db_now() + timedelta(minutes=5)
    assert db.prune_api_platform(now=ahead)["api_idempotency"] == 1  # a driven clock is obeyed
    db.claim_idempotency(project["id"], "/v1/responses", "idem-clock", "fp")
    with db.connection() as con:
        con.execute(
            "UPDATE api_idempotency SET expires_at = now() + interval '30 seconds' "
            "WHERE idem_key = 'idem-clock'"
        )
    assert db.prune_api_platform()["api_idempotency"] == 0
    assert db.get_idempotency(project["id"], "/v1/responses", "idem-clock") is not None
