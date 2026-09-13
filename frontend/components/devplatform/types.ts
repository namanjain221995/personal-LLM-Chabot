/**
 * The developer console's wire contract.
 *
 * These are the shapes the console API answers with — the session-authenticated
 * surface behind /api/devplatform/*, NOT the public /v1 surface, which speaks
 * the CONTRACT §9 envelope and is never reached from a browser page.
 *
 * `number | null` is load-bearing wherever it appears, exactly as it is in the
 * analytics console (components/admin/analytics/types.ts): null means the
 * platform did not measure that value and the UI renders "—". CONTRACT §9 is
 * explicit about the reason — `llm.get_usage()` returns None for "not
 * measured", and a zero there would be both a lie and an under-charge. Nothing
 * in this console may invent a number.
 *
 * Timestamps are ISO-8601 strings or null; the console formats them with
 * lib/format so the whole product renders a moment the same way.
 */

/**
 * A project's ceilings, exactly as `console_api._limits_payload` sends them.
 *
 * NULL INHERITS, ZERO MEANS ZERO (the shared limits rule, 2026-09-13). A null
 * here is "no value of its own — the platform default applies", and the
 * console says that in words; it never renders null as 0, because a 0 is a
 * real limit that allows nothing.
 */
export interface ProjectLimits {
  rpm: number | null;
  input_tpm: number | null;
  output_tpm: number | null;
  max_concurrency: number | null;
  daily_token_quota: number | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
}

/**
 * V34 `api_projects`, as `console_api._project_payload` sends it.
 *
 * FIXED 2026-09-13: this shape used to carry flat, non-null limit columns and
 * an active-key count the orchestrator never sends, so the detail dialog
 * called `.toLocaleString()` on undefined the moment a real row arrived.
 */
export interface Project {
  id: string;
  name: string;
  environment: 'test' | 'live';
  status: 'active' | 'disabled';
  allowed_models: string[];
  allowed_origins: string[];
  ip_allowlist: string[];
  retention_days: number | null;
  limits: ProjectLimits;
  created_at: string | null;
  disabled_at: string | null;
}

/**
 * V34 `api_keys`, minus every column that could reconstruct the secret
 * (`console_api._key_payload`).
 *
 * `public_id` is the lookup half and is safe to show and to log (CONTRACT §5);
 * `last_four` exists so a person can recognise a key they are about to revoke.
 * There is no field here that carries the secret, because there is no moment
 * after creation at which the server could return it — see CreatedKey.
 */
export interface ApiKey {
  id: string;
  project_id: string;
  name: string;
  public_id: string;
  last_four: string;
  environment: 'test' | 'live';
  scopes: string[];
  status: string;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

/**
 * The ONLY shape that ever carries a plaintext key, and only as the direct
 * answer to the POST that minted it.
 *
 * It is deliberately not part of ApiKey: a type that can hold a secret is a
 * type somebody eventually stores. This one is read once by CreateKeyDialog,
 * shown once, and dropped when the dialog closes.
 */
export interface CreatedKey {
  key: ApiKey;
  secret: string;
}

/**
 * The code-level registry (CONTRACT §15) as `PublicModel.to_wire()` writes it,
 * plus the database's narrowing flag.
 */
export interface ConsoleModel {
  id: string;
  status: string;
  /** False when a `public_models` row disabled it. The DB can only narrow. */
  enabled: boolean;
  capabilities: {
    chat: boolean;
    streaming: boolean;
    vision: boolean;
    tools: boolean;
    embeddings?: boolean;
  };
  max_input_tokens: number | null;
  max_output_tokens: number | null;
}

export interface ModelList {
  models: ConsoleModel[];
  can_manage: boolean;
}

/** One day of `GET /usage`. The ledger sums counts, so these are measured. */
export interface UsageDay {
  day: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  errors: number;
  rate_limited: number;
}

export interface UsageReport {
  range: { days: number; start: string; end: string };
  series: UsageDay[];
  totals: {
    requests: number;
    input_tokens: number;
    output_tokens: number;
    errors: number;
    rate_limited: number;
    total_tokens: number;
  };
  projects: {
    id: string;
    name: string;
    requests: number;
    input_tokens: number;
    output_tokens: number;
    errors: number;
  }[];
}

/**
 * V34 `api_responses`, metadata only (`console_api._log_payload`).
 *
 * CONTRACT §16: prompt and output content are NOT stored by default, so there
 * is no `input` or `output` field here and the panel must never offer to show
 * one. What a request log holds is the shape of the request, not its words.
 */
export interface RequestLogRow {
  id: string;
  request_id: string;
  created_at: string | null;
  model: string;
  status: string;
  /** Empty string when the request did not fail. */
  error_code: string;
  streamed: boolean;
  background: boolean;
  key: { id: string; name: string; last_four: string } | null;
  input_tokens: number | null;
  output_tokens: number | null;
  ttft_ms: number | null;
  duration_ms: number | null;
}

export interface RequestLogPage {
  project: { id: string; name: string };
  requests: RequestLogRow[];
}

/**
 * V34 `api_webhook_endpoints` (`console_api._webhook_payload`).
 *
 * The signing secret is NEVER in this shape, not even on creation: the
 * orchestrator generates it, keeps it, and returns only `has_secret`. There is
 * therefore no CreatedWebhook type — a console that displayed a "show once"
 * secret here would be displaying something the server never sent.
 */
export interface WebhookEndpoint {
  id: string;
  project_id: string;
  url: string;
  events: string[];
  status: 'active' | 'disabled';
  include_output: boolean;
  has_secret: boolean;
  rotation_in_progress: boolean;
  created_at: string | null;
  last_delivery_at: string | null;
  last_delivery_status: string;
  consecutive_failures: number;
  disabled_at: string | null;
}

/** What `GET /overview` answers: enough for the landing panel. */
export interface ConsoleOverview {
  workspace: { id: string; name: string };
  stats: {
    projects: number;
    active_projects: number;
    keys: number;
    active_keys: number;
    models: number;
    today: {
      day: string;
      requests: number;
      input_tokens: number;
      output_tokens: number;
      errors: number;
    };
  };
  capabilities: Record<string, boolean>;
}

/**
 * The scopes a key may hold — the whole closed vocabulary, in the order the
 * server declares it.
 *
 * MIRRORED, NOT INVENTED. `orchestrator/app/apiplatform/scopes.py` holds
 * `Scope` and `SCOPE_DESCRIPTIONS`, and the hints below are its sentences
 * word for word. A console that writes its own descriptions of a server's
 * permissions is a console that eventually describes them wrongly, and the
 * person ticking the boxes has no way to tell. Adding a scope means adding it
 * there first; anything this list offers that the server does not know is an
 * `UnknownScopeError` at creation time.
 */
export const SCOPES: { id: string; label: string; hint: string }[] = [
  { id: 'models.read', label: 'Read models', hint: 'List the models this key may use.' },
  { id: 'responses.read', label: 'Read responses', hint: 'Read responses created by this project.' },
  { id: 'responses.write', label: 'Create responses', hint: 'Create and cancel responses.' },
  { id: 'usage.read', label: 'Read usage', hint: 'Read this project’s usage counters.' },
];

/** Webhook events a project may subscribe to (CONTRACT §14). */
export const WEBHOOK_EVENTS = [
  'response.completed',
  'response.failed',
  'response.cancelled',
] as const;

/**
 * The environment badge's words.
 *
 * `tsk_live_` and `tsk_test_` are distinguishable at a glance by design
 * (CONTRACT §5) and the console says the same thing in words rather than
 * leaving the prefix to carry it alone — colour is never the only signal.
 */
export const ENVIRONMENT_LABEL: Record<string, string> = {
  live: 'Live',
  test: 'Test',
};
