/**
 * Context-meter maths (Phase C) — pure, so the thresholds are testable
 * without rendering anything.
 *
 * The value is "how full is the NEXT request", not "how big is this chat":
 *   fraction = tokens the next request will use ÷ usable budget
 *   usable   = window − reserved output − safety margin
 *
 * Exact numbers come from the server after each reply (meta.context); while
 * the user types we only add a cheap character estimate for the draft, which
 * is the one place an estimate is acceptable.
 */

import type { ChatMessage, ContextUsage } from './types';

/**
 * The last-known usage for a conversation, read from its own messages.
 *
 * Deriving it from history rather than keeping a map in memory is what makes
 * the meter correct PER SESSION: opening a chat this tab never streamed — or
 * any chat after a reload — still shows that chat's real value, because the
 * reading rode out on its last reply's meta and history already persists meta.
 */
export function latestUsage(messages: ChatMessage[]): ContextUsage | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const usage = messages[i]?.meta?.context;
    if (usage) return usage;
  }
  return null;
}

export type MeterState = 'calm' | 'warn' | 'high' | 'critical';

export const WARN_AT = 0.6;
export const HIGH_AT = 0.85;
export const PULSE_AT = 0.95;

/**
 * Budget assumed before a session's first reply, when the server has not yet
 * reported anything. Mirrors the orchestrator defaults (131072 window − 8192
 * reserved output − 512 margin). Without it a brand-new chat could not show a
 * draft's cost at all, which is exactly when someone pastes a huge document.
 * The first reply replaces these with the served numbers.
 */
export const DEFAULT_RESERVED_OUTPUT = 8192;
export const DEFAULT_USABLE_BUDGET = 131072 - DEFAULT_RESERVED_OUTPUT - 512;

/** Rough tokens for text the user has typed but not sent. */
export function estimateDraftTokens(text: string): number {
  return Math.ceil((text || '').length / 4);
}

export function meterState(fraction: number): MeterState {
  if (!Number.isFinite(fraction) || fraction < 0) return 'calm';
  if (fraction >= PULSE_AT) return 'critical';
  if (fraction >= HIGH_AT) return 'high';
  if (fraction >= WARN_AT) return 'warn';
  return 'calm';
}

/** Ring colour per state. Gray → amber → red, matching the app's tokens. */
export function meterColor(state: MeterState): string {
  switch (state) {
    case 'critical':
    case 'high':
      return 'var(--ts-danger)';
    case 'warn':
      return 'var(--ts-warn)';
    default:
      return 'var(--ts-text-faint)';
  }
}

export function meterPercent(fraction: number): number {
  if (!Number.isFinite(fraction) || fraction <= 0) return 0;
  return Math.min(100, Math.round(fraction * 100));
}

export interface MeterView {
  fraction: number;
  percent: number;
  state: MeterState;
  pulsing: boolean;
  tokensUsed: number;
  usableBudget: number;
  breakdown: { label: string; tokens: number; heldBack?: boolean }[];
}

/**
 * Combine the server's last exact reading with the current draft.
 *
 * `usage` is null before the session's first reply — the meter then shows the
 * draft alone against a full budget rather than pretending to know more.
 */
export function meterView(
  usage: ContextUsage | null,
  draft: string,
): MeterView {
  const draftTokens = estimateDraftTokens(draft);
  const usable = usage?.usable_budget || DEFAULT_USABLE_BUDGET;
  const used = (usage?.tokens_used ?? 0) + draftTokens;
  const fraction = usable > 0 ? used / usable : 0;
  const state = meterState(fraction);
  return {
    fraction,
    percent: meterPercent(fraction),
    state,
    pulsing: fraction >= PULSE_AT,
    tokensUsed: used,
    usableBudget: usable,
    breakdown: buildBreakdown(usage, draftTokens),
  };
}

/**
 * Rows for the popover. The parts the server reports exactly are shown as
 * such; everything the server lumps into the prompt is one honest "Messages
 * and context" row rather than invented per-section numbers.
 */
export function buildBreakdown(
  usage: ContextUsage | null,
  draftTokens: number,
): { label: string; tokens: number; heldBack?: boolean }[] {
  const prompt = usage?.tokens_used ?? 0;
  return [
    { label: 'Messages and context', tokens: prompt },
    { label: 'Your draft', tokens: draftTokens },
    {
      label: 'Reserved for reply',
      tokens: usage?.reserved_output || DEFAULT_RESERVED_OUTPUT,
      // Shown for context, NOT added to the total. The budget it is compared
      // against (usable = window − reserved − margin) already has it taken
      // out, so summing it in counts the same tokens twice: the tooltip read
      // 16,747 while the ring beside it read 3% of the same conversation.
      heldBack: true,
    },
  ].filter((row) => row.tokens > 0);
}

/** The popover total must equal what the rows add up to. */
export function breakdownTotal(
  rows: { label: string; tokens: number; heldBack?: boolean }[],
): number {
  // Only what the NEXT request actually sends. Rows marked heldBack are
  // already excluded from the usable budget this total is compared with.
  return rows.reduce((sum, r) => (r.heldBack ? sum : sum + r.tokens), 0);
}

/* -------------------------------------------------------------------------
 * "Compact now" — what the button may claim before it is pressed.
 *
 * The control used to look equally actionable whether or not anything could
 * be folded, and said nothing lasting once it had run. The counts below come
 * from the server (the summary endpoint, and the compact response itself), so
 * the label can only ever promise what the button will actually do.
 * ---------------------------------------------------------------------- */

/** The server's answer to "what would a compaction fold right now?". */
export interface FoldableCounts {
  /** Turns an on-demand compaction would fold at this moment. */
  foldableTurns: number;
  /** Turns in the conversation, folded or not. */
  totalTurns: number;
}

/**
 * Read the counts off a server payload — the summary GET or the compact POST.
 *
 * Returns null for anything it cannot trust (missing fields, an older
 * orchestrator, a mock response, garbage). Null means UNKNOWN, and unknown
 * deliberately keeps the button enabled: a server that cannot answer must not
 * be able to disable a control that still works.
 */
export function readFoldableCounts(body: unknown): FoldableCounts | null {
  if (!body || typeof body !== 'object') return null;
  const raw = body as Record<string, unknown>;
  const foldable = raw.foldable_turns;
  const total = raw.total_turns;
  if (typeof foldable !== 'number' || !Number.isFinite(foldable)) return null;
  if (typeof total !== 'number' || !Number.isFinite(total)) return null;
  if (foldable < 0 || total < 0) return null;
  return {
    foldableTurns: Math.floor(foldable),
    totalTurns: Math.floor(total),
  };
}

export interface CompactPlanInput {
  /** Turns that would be folded now; null when the server has not said. */
  foldable: number | null;
  /** Turns the last successful compaction folded, this session. */
  lastFolded?: number | null;
  /** A compaction request is in flight. */
  compacting?: boolean;
  /** The host already forbids it — no chat open, or a stream is running. */
  blocked?: boolean;
}

export interface CompactPlan {
  label: string;
  disabled: boolean;
  /** The line under the button: what it will do, or why it cannot. */
  hint: string | null;
  /** Lasting record of the last compaction; null until one succeeds. */
  folded: string | null;
  /** Whether to offer the way back into the existing SummaryPanel. */
  showSummaryLink: boolean;
  summaryLabel: string;
}

/** Why the button is dead when there is genuinely nothing to fold. */
export const NOTHING_TO_COMPACT =
  'Nothing to compact yet — earlier turns are folded automatically as the window fills.';

/** "1 earlier message" / "12 earlier messages". */
export function earlierMessages(count: number): string {
  return `${count} earlier message${count === 1 ? '' : 's'}`;
}

/**
 * Everything the popover renders about compaction, decided here so it is
 * testable in node and the component stays a shell.
 */
export function compactPlan({
  foldable,
  lastFolded = null,
  compacting = false,
  blocked = false,
}: CompactPlanInput): CompactPlan {
  const known = typeof foldable === 'number' && Number.isFinite(foldable);
  const count = known ? Math.max(0, Math.floor(foldable as number)) : null;
  const nothing = count === 0;

  const folded =
    typeof lastFolded === 'number' && lastFolded > 0
      ? `Compacted ${earlierMessages(Math.floor(lastFolded))}`
      : null;

  return {
    label: compacting ? 'Compacting…' : 'Compact now',
    disabled: blocked || compacting || nothing,
    hint: nothing
      ? NOTHING_TO_COMPACT
      : count !== null
        ? `Folds ${earlierMessages(count)} into a summary.`
        : // Unknown: promise nothing rather than guess, and stay clickable.
          null,
    folded,
    showSummaryLink: folded !== null,
    summaryLabel: 'See what was kept',
  };
}
