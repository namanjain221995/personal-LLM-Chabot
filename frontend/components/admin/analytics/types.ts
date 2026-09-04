/**
 * The analytics console's wire contract (orchestrator authn/analytics_api.py).
 *
 * `number | null` is load-bearing everywhere it appears: null means the
 * backend never measured that value, and the UI must render it as "—" rather
 * than 0. Nothing here is optional-by-accident — if a field can be missing,
 * the type says so.
 */

export type RangeKey = '1h' | '24h' | '7d' | '30d' | '90d';

export const RANGES: { key: RangeKey; label: string; long: string }[] = [
  { key: '1h', label: 'Last hour', long: 'the last hour' },
  { key: '24h', label: 'Last 24 hours', long: 'the last 24 hours' },
  { key: '7d', label: 'Last 7 days', long: 'the last 7 days' },
  { key: '30d', label: 'Last 30 days', long: 'the last 30 days' },
  { key: '90d', label: 'Last 90 days', long: 'the last 90 days' },
];

export interface Window {
  key: RangeKey;
  hours: number;
  bucket: 'hour' | 'day';
  since: string;
  until: string;
  previous_since: string;
  previous_until: string;
}

/** When telemetry actually starts — what stops a chart implying a collapse. */
export interface Coverage {
  first_event: string | null;
  last_event: string | null;
  events: number;
}

export interface Totals {
  requests: number;
  ok: number;
  errors: number;
  cancelled: number;
  users: number;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  avg_ttft_ms: number | null;
  p50_ttft_ms: number | null;
  p95_ttft_ms: number | null;
  p99_ttft_ms: number | null;
  avg_duration_ms: number | null;
  p50_duration_ms: number | null;
  p95_duration_ms: number | null;
  p99_duration_ms: number | null;
  avg_tokens_per_second: number | null;
}

export interface MemberUsage {
  id: number;
  name: string;
  email: string;
  role: 'super_admin' | 'admin' | 'member';
  status: string;
  last_active_at: string | null;
  requests: number;
  errors: number;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  avg_ttft_ms: number | null;
  messages: number;
  conversations: number;
  research_runs: number;
  web_searches: number;
  /**
   * How many times this person dictated. Only the voice endpoint counts it,
   * so it is absent — not zero — on every other endpoint's rows.
   */
  transcriptions?: number;
}

export interface UsagePoint {
  bucket: string;
  requests: number;
  errors: number;
  users: number;
  input_tokens: number | null;
  output_tokens: number | null;
  avg_ttft_ms: number | null;
}

export interface ActivePoint {
  bucket: string;
  active: number;
  messages: number;
  chat: number;
  research: number;
  web_search: number;
  salesforce: number;
}

export interface RouteUsage {
  route: string;
  requests: number;
  errors: number;
  output_tokens?: number | null;
  avg_ttft_ms: number | null;
  avg_duration_ms?: number | null;
}

export interface ModelUsage {
  model: string;
  requests: number;
  share: number | null;
  errors?: number;
  cancelled?: number;
  users?: number;
  input_tokens?: number | null;
  output_tokens: number | null;
  avg_ttft_ms: number | null;
  p95_ttft_ms?: number | null;
  avg_duration_ms?: number | null;
  avg_tokens_per_second: number | null;
}

export interface Overview {
  workspace: { id: string; name: string };
  range: Window;
  coverage: Coverage;
  /** Every model that has served a request here — the filter's options. */
  available_models: string[];
  /** The model currently filtered to, or '' for all of them. */
  model: string;
  totals: Totals;
  previous: Totals;
  deltas: Record<string, number | null>;
  chat: {
    messages: number;
    answers: number;
    conversations: number;
    new_conversations: number;
    users: number;
  };
  series: { usage: UsagePoint[]; active_users: ActivePoint[] };
  routes: RouteUsage[];
  models: ModelUsage[];
  top_users: MemberUsage[];
}

export interface Leaderboard {
  range: Window;
  order: string;
  limit: number;
  offset: number;
  total: number;
  rows: MemberUsage[];
}

export interface ChatAnalytics {
  range: Window;
  totals: {
    messages: number;
    answers: number;
    conversations: number;
    new_conversations: number;
    users: number;
    thumbs_up: number;
    thumbs_down: number;
    messages_per_conversation: number | null;
  };
  deltas: Record<string, number | null>;
  series: {
    bucket: string;
    messages: number;
    answers: number;
    conversations: number;
  }[];
  routes: { route: string; requests: number }[];
  top_users: MemberUsage[];
}

export interface ResearchAnalytics {
  range: Window;
  totals: {
    runs: number;
    completed: number;
    failed: number;
    cancelled: number;
    running: number;
    users: number;
    queries: number;
    citations: number;
    success_rate: number | null;
    avg_iterations: number | null;
    avg_queries: number | null;
    avg_sources_found: number | null;
    avg_sources_cited: number | null;
    avg_seconds: number | null;
    p95_seconds: number | null;
    avg_report_chars: number | null;
  };
  deltas: Record<string, number | null>;
  series: { bucket: string; runs: number; completed: number; failed: number }[];
  top_users: MemberUsage[];
}

export interface SearchAnalytics {
  range: Window;
  totals: {
    searches: number;
    queries: number;
    users: number;
    results: number;
    unique_urls: number;
    pages_fetched: number;
    domains: number;
    results_per_search: number | null;
  };
  deltas: Record<string, number | null>;
  series: { bucket: string; searches: number }[];
  providers: { provider: string; searches: number }[];
  domains: { domain: string; pages: number }[];
  top_users: MemberUsage[];
}

export interface SalesforceAnalytics {
  range: Window;
  totals: {
    answers: number;
    users: number;
    failed: number;
    live: number;
    synced: number;
    sql_route: number;
    dataset_route: number;
    intents: number;
    success_rate: number | null;
  };
  deltas: Record<string, number | null>;
  series: { bucket: string; answers: number; live: number }[];
}

export interface VoiceAnalytics {
  range: Window;
  /**
   * Voice over ALL time, not the window. It is what tells "nobody dictated
   * this fortnight" apart from "voice has never been used here" — two empty
   * pages that deserve to say different things.
   */
  coverage: {
    first_transcription: string | null;
    last_transcription: string | null;
    transcriptions: number;
  };
  totals: {
    transcriptions: number;
    users: number;
    ok: number;
    failed: number;
    busy: number;
    rejected: number;
    unavailable: number;
    error: number;
    /** Attempts the fallback engine answered — a quiet kind of outage. */
    degraded: number;
    success_rate: number | null;
    /** Null when no clip in the window reported a length. Never 0. */
    total_duration_ms: number | null;
    total_minutes: number | null;
    avg_duration_ms: number | null;
    p95_duration_ms: number | null;
    /** The wait the person sat through, failures included. */
    avg_processing_ms: number | null;
    p95_processing_ms: number | null;
    languages: number;
    /** Clips whose language the model actually named. */
    language_identified: number;
  };
  deltas: Record<string, number | null>;
  series: {
    bucket: string;
    transcriptions: number;
    ok: number;
    failed: number;
  }[];
  languages: {
    language: string;
    transcriptions: number;
    users: number;
    /** Share of the clips that were IDENTIFIED, not of every clip. */
    share: number | null;
  }[];
  top_users: MemberUsage[];
}

/** A vLLM engine as Prometheus sees it. Every metric may be absent. */
export interface Engine {
  service: string;
  model: string;
  node: string;
  instance: string;
  running: number | null;
  waiting: number | null;
  kv_cache_percent: number | null;
  prompt_tokens_per_second: number | null;
  generation_tokens_per_second: number | null;
  prompt_tokens_total: number | null;
  generation_tokens_total: number | null;
  avg_ttft_seconds: number | null;
  avg_e2e_seconds: number | null;
  avg_queue_seconds: number | null;
  avg_inter_token_seconds: number | null;
  finished_requests: number | null;
  preemptions: number | null;
  prefix_cache_hit_rate: number | null;
}

export interface NodeState {
  node: string;
  role: string;
  gpu_present: boolean;
  gpu_up: boolean;
  node_up: boolean;
  gpu_utilization: number | null;
  gpu_memory_bytes: number | null;
  gpu_temperature_c: number | null;
  gpu_power_w: number | null;
  gpu_throttled: boolean;
  gpu_processes: number | null;
  gpu_clock_hz: number | null;
  cpu_percent: number | null;
  load1: number | null;
  memory_total_bytes: number | null;
  memory_used_bytes: number | null;
  swap_used_bytes: number | null;
  uptime_seconds: number | null;
  network_rx_bps: number | null;
  network_tx_bps: number | null;
}

/**
 * Every infrastructure block is either available or explains itself. There is
 * no third state and no default of zero — see analytics/infra.py.
 */
export type Infra<T> =
  | ({ available: true } & T)
  | { available: false; reason: string; source: string };

export interface RangeSeries {
  points: [number, number | null][];
}

export interface Infrastructure {
  hours: number;
  nodes: Infra<{ nodes: NodeState[] }>;
  engines: Infra<{ engines: Engine[] }>;
  gpu_series: Infra<{
    hours: number;
    series: Record<string, ({ node: string } & RangeSeries)[]>;
  }>;
}

export interface ModelsAnalytics {
  range: Window;
  coverage: Coverage;
  models: ModelUsage[];
  effort: {
    effort: string;
    requests: number;
    avg_ttft_ms: number | null;
    avg_duration_ms: number | null;
    output_tokens: number | null;
  }[];
  engines: Infra<{ engines: Engine[] }>;
}

export interface PerformanceAnalytics {
  range: Window;
  available_models: string[];
  model: string;
  totals: Totals;
  previous: Totals;
  deltas: Record<string, number | null>;
  series: {
    bucket: string;
    requests: number;
    errors: number;
    avg_ttft_ms: number | null;
  }[];
  ttft_histogram: { label: string; count: number }[];
  routes: RouteUsage[];
  engines: Infra<{ engines: Engine[] }>;
  engine_series: Infra<{
    hours: number;
    series: Record<string, ({ service: string } & RangeSeries)[]>;
  }>;
}

/** Human labels for the engine routes stored in usage_events.route. */
export const ROUTE_LABEL: Record<string, string> = {
  chat: 'Chat',
  vision: 'Vision',
  agent: 'Agent',
  clarify: 'Clarify',
  search: 'Web search',
  url: 'URL read',
  deep_research: 'Deep research',
  sql: 'Salesforce',
  dataset: 'Dataset',
  rag: 'Documents',
  unknown: 'Unknown',
};

export function routeLabel(route: string): string {
  return ROUTE_LABEL[route] ?? route.replace(/_/g, ' ');
}

/** Human labels for the effort tiers the composer offers. */
export const EFFORT_LABEL: Record<string, string> = {
  fast: 'Fast',
  think: 'Think',
  max: 'Max',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  extra_high: 'Extra high',
  unset: 'Unset',
};
