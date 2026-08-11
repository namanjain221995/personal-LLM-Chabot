/**
 * Progress phases — mirrors `orchestrator/app/core/sf_intel/phases.py`.
 *
 * The indicator is driven ENTIRELY by what the backend emits. There is no
 * timer walking through plausible-looking steps, and no phase is ever inferred:
 * if the server has not said it is querying Salesforce, the UI does not say so
 * either. That is the difference between a progress indicator and a decoration.
 */

export const PHASES = [
  'understanding',
  'resolving_context',
  'checking_schema',
  'clarifying',
  'querying_salesforce',
  'retrieving_more_results',
  'analyzing_records',
  'calculating',
  'verifying',
  'drafting_answer',
  'reconnecting',
  'completed',
  'failed',
] as const;

export type Phase = (typeof PHASES)[number];

/** Live status carried on the `status` SSE event and the final `meta`. */
export interface PhaseStatus {
  phase: Phase;
  label: string;
  run_id?: string;
  started_at?: string;
  record_count?: number;
  tool_name?: string;
}

const PHASE_SET = new Set<string>(PHASES);

/** Phases during which the star animates. Terminal ones stop it. */
const ACTIVE = new Set<Phase>(
  PHASES.filter((p) => p !== 'completed' && p !== 'failed' && p !== 'clarifying'),
);

export function isPhase(value: unknown): value is Phase {
  return typeof value === 'string' && PHASE_SET.has(value);
}

export function isActivePhase(phase: Phase | undefined): boolean {
  return phase !== undefined && ACTIVE.has(phase);
}

/**
 * The visual family a phase belongs to. Six states, not thirteen: the star
 * changes character when the KIND of work changes, and a user watching
 * "Analyzing 42 records" become "Verifying the totals" should not see the
 * animation restart.
 */
export type StarState =
  | 'understanding'
  | 'searching'
  | 'calculating'
  | 'verifying'
  | 'drafting'
  | 'reconnecting';

const STAR_STATE: Record<Phase, StarState | null> = {
  understanding: 'understanding',
  resolving_context: 'understanding',
  checking_schema: 'searching',
  clarifying: null,
  querying_salesforce: 'searching',
  retrieving_more_results: 'searching',
  analyzing_records: 'calculating',
  calculating: 'calculating',
  verifying: 'verifying',
  drafting_answer: 'drafting',
  reconnecting: 'reconnecting',
  completed: null,
  failed: null,
};

export function starState(phase: Phase | undefined): StarState | null {
  return phase ? STAR_STATE[phase] : null;
}

/**
 * Screen-reader text. Announced through aria-live, so it says what is HAPPENING
 * rather than restating the visible label word for word.
 */
export function accessibleStatus(status: PhaseStatus | null): string {
  if (!status) return '';
  if (status.phase === 'completed') return 'Answer ready.';
  if (status.phase === 'failed') return 'The request did not complete.';
  return `Salesforce assistant is processing the request: ${status.label}`;
}

/**
 * Parse a `status` SSE payload into a typed phase, or null.
 *
 * `text` alone (no `phase`) is the pre-existing web-search/URL progress line —
 * it stays a plain string and does NOT drive the star, so the two progress
 * systems cannot fight over the same row.
 */
export function parsePhaseStatus(raw: unknown): PhaseStatus | null {
  if (!raw || typeof raw !== 'object') return null;
  const s = raw as Record<string, unknown>;
  if (!isPhase(s.phase)) return null;
  return {
    phase: s.phase,
    label: typeof s.text === 'string' ? s.text : s.phase,
    ...(typeof s.run_id === 'string' ? { run_id: s.run_id } : {}),
    ...(typeof s.started_at === 'string' ? { started_at: s.started_at } : {}),
    ...(typeof s.record_count === 'number'
      ? { record_count: s.record_count }
      : {}),
    ...(typeof s.tool_name === 'string' ? { tool_name: s.tool_name } : {}),
  };
}
