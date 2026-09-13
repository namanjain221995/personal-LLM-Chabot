# V34 — the developer platform schema (authoritative column list)

One migration, purely additive, appended as `_MIGRATION_V34` with `(34,
_MIGRATION_V34)` in `_MIGRATIONS` (`orchestrator/app/db.py`). House style is
mandatory: `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, named
`CHECK` constraints inline (`<table>_<column>`), `idx_<table>_<purpose>` index
names, `timestamptz NOT NULL DEFAULT now()`, jsonb defaults `'{}'::jsonb` /
`'[]'::jsonb`.

Two rules that apply to every table below:

* **Tenancy is explicit.** Every table carries `workspace_id text NOT NULL
  REFERENCES workspaces(id) ON DELETE CASCADE`, even where it could be derived
  by a join — the audit found workspace scoping today is reconstructed rather
  than stored, and a quota query must never have to guess.
* **Every foreign key that a cascade walks gets an index**, which V29 and V30
  forgot and V31 states as the rule.

No column stores a plaintext API key. No column stores prompt or completion text
except `api_responses.output_text`, which exists only so a background response
can be fetched later and is subject to retention.

## Tables

### `api_projects`
`id text PRIMARY KEY` (`proj_<24 hex>`) · `workspace_id` · `name text NOT NULL` ·
`environment text NOT NULL CHECK (environment IN ('test','live'))` ·
`status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled'))` ·
`allowed_models jsonb NOT NULL DEFAULT '[]'::jsonb` ·
`allowed_origins jsonb NOT NULL DEFAULT '[]'::jsonb` ·
`ip_allowlist jsonb NOT NULL DEFAULT '[]'::jsonb` ·
`rpm integer NOT NULL DEFAULT 60` · `input_tpm bigint NOT NULL DEFAULT 200000` ·
`output_tpm bigint NOT NULL DEFAULT 60000` · `max_concurrency integer NOT NULL DEFAULT 4` ·
`daily_token_quota bigint NOT NULL DEFAULT 2000000` ·
`max_input_tokens integer` · `max_output_tokens integer` ·
`retention_days integer NOT NULL DEFAULT 30` ·
`metadata jsonb NOT NULL DEFAULT '{}'::jsonb` ·
`created_by integer REFERENCES users(id) ON DELETE SET NULL` ·
`created_at` · `disabled_at timestamptz`
Indexes: `idx_api_projects_workspace (workspace_id, created_at DESC)`,
`idx_api_projects_created_by (created_by)`,
unique `idx_api_projects_name (workspace_id, lower(name))`.

### `api_service_accounts`
`id text PRIMARY KEY` (`svc_<24 hex>`) · `project_id text NOT NULL REFERENCES api_projects(id) ON DELETE CASCADE` ·
`workspace_id` · `name text NOT NULL` · `description text NOT NULL DEFAULT ''` ·
`status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled'))` ·
`scopes jsonb NOT NULL DEFAULT '[]'::jsonb` · `allowed_models jsonb NOT NULL DEFAULT '[]'::jsonb` ·
`created_by` · `created_at` · `last_used_at timestamptz` · `disabled_at timestamptz`
Indexes: `idx_api_service_accounts_project (project_id)`, `idx_api_service_accounts_workspace (workspace_id)`.

### `api_keys`
`id text PRIMARY KEY` (`key_<24 hex>`) · `public_id text NOT NULL UNIQUE` ·
`key_hash text NOT NULL` (HMAC-SHA256 hex) · `last_four text NOT NULL` ·
`project_id text NOT NULL REFERENCES api_projects(id) ON DELETE CASCADE` ·
`service_account_id text REFERENCES api_service_accounts(id) ON DELETE CASCADE` ·
`workspace_id` · `environment text NOT NULL CHECK (environment IN ('test','live'))` ·
`name text NOT NULL` · `scopes jsonb NOT NULL DEFAULT '[]'::jsonb` ·
`allowed_models jsonb NOT NULL DEFAULT '[]'::jsonb` ·
`rpm integer` · `max_concurrency integer` (null = inherit the project) ·
`status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked'))` ·
`expires_at timestamptz` · `created_by` · `created_at` ·
`last_used_at timestamptz` · `last_used_ip text` ·
`revoked_at timestamptz` · `revoked_by integer REFERENCES users(id) ON DELETE SET NULL` ·
`rotated_from text` · `rotation_expires_at timestamptz`
Indexes: `idx_api_keys_project (project_id)`, `idx_api_keys_workspace (workspace_id)`,
`idx_api_keys_service_account (service_account_id)`, `idx_api_keys_created_by (created_by)`,
`idx_api_keys_revoked_by (revoked_by)`,
partial `idx_api_keys_active (public_id) WHERE status = 'active'`.

### `api_responses`
`id text PRIMARY KEY` (`resp_<24 hex>`) · `project_id` (FK cascade) · `key_id text REFERENCES api_keys(id) ON DELETE SET NULL` ·
`workspace_id` · `model text NOT NULL` ·
`status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','in_progress','completed','failed','cancelled'))` ·
`background boolean NOT NULL DEFAULT false` · `streamed boolean NOT NULL DEFAULT false` ·
`request_id text NOT NULL` · `fingerprint text NOT NULL DEFAULT ''` ·
`instructions_present boolean NOT NULL DEFAULT false` ·
`input_tokens integer` · `output_tokens integer` (NULL means **not measured**, never 0) ·
`ttft_ms integer` · `duration_ms integer` ·
`error_code text` · `error_message text` ·
`output_text text` (background only; pruned by retention) ·
`cancel_requested boolean NOT NULL DEFAULT false` ·
`metadata jsonb NOT NULL DEFAULT '{}'::jsonb` ·
`created_at` · `started_at timestamptz` · `completed_at timestamptz` · `expires_at timestamptz`
Indexes: `idx_api_responses_project (project_id, created_at DESC)`,
`idx_api_responses_workspace (workspace_id, created_at DESC)`,
`idx_api_responses_key (key_id)`,
partial `idx_api_responses_open (status, created_at) WHERE status IN ('queued','in_progress')`,
`idx_api_responses_expires (expires_at) WHERE expires_at IS NOT NULL`.

### `api_idempotency`
`id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY` · `project_id` (FK cascade) ·
`endpoint text NOT NULL` · `idem_key text NOT NULL` · `fingerprint text NOT NULL` ·
`response_id text REFERENCES api_responses(id) ON DELETE CASCADE` ·
`state text NOT NULL DEFAULT 'in_flight' CHECK (state IN ('in_flight','completed'))` ·
`created_at` · `expires_at timestamptz NOT NULL`
Indexes: unique `idx_api_idempotency_claim (project_id, endpoint, idem_key)`,
`idx_api_idempotency_expires (expires_at)`, `idx_api_idempotency_response (response_id)`.

### `api_usage_minute`
`project_id` · `key_id text` · `bucket timestamptz NOT NULL` (minute truncated) ·
`requests integer NOT NULL DEFAULT 0` · `input_tokens bigint NOT NULL DEFAULT 0` ·
`output_tokens bigint NOT NULL DEFAULT 0`
`PRIMARY KEY (project_id, key_id, bucket)`; index `idx_api_usage_minute_bucket (bucket)` for pruning.

### `api_usage_daily`
`project_id` · `day date NOT NULL` · `requests integer NOT NULL DEFAULT 0` ·
`input_tokens bigint NOT NULL DEFAULT 0` · `output_tokens bigint NOT NULL DEFAULT 0` ·
`errors integer NOT NULL DEFAULT 0` · `rate_limited integer NOT NULL DEFAULT 0`
`PRIMARY KEY (project_id, day)`; index `idx_api_usage_daily_day (day)`.

### `api_webhook_endpoints`
`id text PRIMARY KEY` (`whe_<24 hex>`) · `project_id` (FK cascade) · `workspace_id` ·
`url text NOT NULL` · `events jsonb NOT NULL DEFAULT '[]'::jsonb` ·
`status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled'))` ·
`secret text NOT NULL` · `previous_secret text` · `previous_secret_expires_at timestamptz` ·
`include_output boolean NOT NULL DEFAULT false` ·
`created_by` · `created_at` · `last_delivery_at timestamptz` · `last_delivery_status text` ·
`consecutive_failures integer NOT NULL DEFAULT 0` · `disabled_at timestamptz`
Index: `idx_api_webhook_endpoints_project (project_id)`.
(The signing secret is stored because the server must compute the signature; it
is shown once in the console and never returned by any API.)

### `api_webhook_deliveries`
`id text PRIMARY KEY` (`whd_<24 hex>`) · `event_id text NOT NULL` · `endpoint_id text NOT NULL REFERENCES api_webhook_endpoints(id) ON DELETE CASCADE` ·
`project_id` · `event_type text NOT NULL` · `response_id text` ·
`payload jsonb NOT NULL DEFAULT '{}'::jsonb` ·
`status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','failed','dropped'))` ·
`attempt integer NOT NULL DEFAULT 0` · `max_attempts integer NOT NULL DEFAULT 6` ·
`http_status integer` · `error text` ·
`next_attempt_at timestamptz` · `created_at` · `delivered_at timestamptz`
Indexes: `idx_api_webhook_deliveries_endpoint (endpoint_id, created_at DESC)`,
partial `idx_api_webhook_deliveries_due (next_attempt_at) WHERE status = 'pending'`,
unique `idx_api_webhook_deliveries_event (endpoint_id, event_id)`.

### `public_models`
`id text PRIMARY KEY` (the public model id declared in code) ·
`enabled boolean NOT NULL DEFAULT true` · `updated_by integer REFERENCES users(id) ON DELETE SET NULL` ·
`updated_at timestamptz NOT NULL DEFAULT now()`
A row can only **disable** a model the code declares. A row for an id the code
does not declare is ignored — the database can never expose a model.

### `platform_secrets`
`name text PRIMARY KEY` · `value text NOT NULL` · `created_at`
Holds the generated API-key pepper when `API_KEY_PEPPER` is not configured, so a
fresh install works without a deploy-time secret. Documented as the weaker of the
two options because the digests live in the same database.

## Notes for the implementer

* Statement timeout is 15 s per connection and the whole migration runs in one
  transaction — every index here is on an empty new table, so nothing can hit it.
* `orchestrator/tests/conftest.py` keeps a hand-maintained truncation list. Every
  table above must be added to it, or V34 tables leak state between tests.
* The CI `schema` job proves fresh-install equals upgrade; the `policy` job
  asserts the migration list is ascending, unique and contiguous with
  `LATEST_SCHEMA_VERSION == max`.
