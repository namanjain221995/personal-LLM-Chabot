import { describe, expect, it } from 'vitest';
import {
  DEFAULT_USABLE_BUDGET,
  HIGH_AT,
  NOTHING_TO_COMPACT,
  PULSE_AT,
  WARN_AT,
  breakdownTotal,
  compactPlan,
  earlierMessages,
  estimateDraftTokens,
  latestUsage,
  meterColor,
  meterPercent,
  meterState,
  meterView,
  readFoldableCounts,
} from '../lib/contextMeter';
import type { ChatMessage, ContextUsage } from '../lib/types';

const usage = (over: Partial<ContextUsage> = {}): ContextUsage => ({
  tokens_used: 0,
  usable_budget: 10000,
  window: 131072,
  reserved_output: 8192,
  fraction: 0,
  summarized_turns: 0,
  ...over,
});

describe('meterState thresholds', () => {
  it('is gray below 60%', () => {
    expect(meterState(0)).toBe('calm');
    expect(meterState(0.599)).toBe('calm');
  });

  it('turns amber EXACTLY at 60%', () => {
    expect(meterState(WARN_AT)).toBe('warn');
    expect(meterState(0.84)).toBe('warn');
  });

  it('turns red EXACTLY at 85%', () => {
    expect(meterState(0.8499)).toBe('warn');
    expect(meterState(HIGH_AT)).toBe('high');
  });

  it('pulses from 95%', () => {
    expect(meterState(0.949)).toBe('high');
    expect(meterState(PULSE_AT)).toBe('critical');
    expect(meterView(usage({ tokens_used: 9500 }), '').pulsing).toBe(true);
    expect(meterView(usage({ tokens_used: 9400 }), '').pulsing).toBe(false);
  });

  it('maps each state to a distinct theme colour', () => {
    expect(meterColor('calm')).toContain('faint');
    expect(meterColor('warn')).toContain('warn');
    expect(meterColor('high')).toContain('danger');
    expect(meterColor('critical')).toContain('danger');
  });
});

describe('meterPercent', () => {
  it('rounds and clamps at 100', () => {
    expect(meterPercent(0.4242)).toBe(42);
    expect(meterPercent(1.9)).toBe(100);
    expect(meterPercent(-1)).toBe(0);
    expect(meterPercent(Number.NaN)).toBe(0);
  });
});

describe('meterView', () => {
  it('combines the server total with the live draft estimate', () => {
    const view = meterView(usage({ tokens_used: 4000 }), 'x'.repeat(400));
    expect(view.tokensUsed).toBe(4000 + estimateDraftTokens('x'.repeat(400)));
    expect(view.percent).toBe(41); // 4100 / 10000
  });

  it('shows 0% before a session has any usage', () => {
    const view = meterView(null, '');
    expect(view.percent).toBe(0);
    expect(view.state).toBe('calm');
  });

  it('still costs a draft against the default budget before the first reply', () => {
    // Otherwise pasting a huge document into a brand-new chat reads as 0%.
    const view = meterView(null, 'x'.repeat(400_000));
    expect(view.percent).toBeGreaterThan(50);
    expect(view.usableBudget).toBe(DEFAULT_USABLE_BUDGET);
  });

  it('the served budget replaces the default once a reply arrives', () => {
    const view = meterView(usage({ usable_budget: 20000, tokens_used: 10000 }), '');
    expect(view.usableBudget).toBe(20000);
    expect(view.percent).toBe(50);
  });

  it('the popover total excludes the held-back reservation', () => {
    const view = meterView(usage({ tokens_used: 4000 }), 'hello there');
    const sent = view.breakdown
      .filter((r) => !r.heldBack)
      .reduce((s, r) => s + r.tokens, 0);
    expect(breakdownTotal(view.breakdown)).toBe(sent);
    expect(view.breakdown.map((r) => r.label)).toEqual([
      'Messages and context',
      'Your draft',
      'Reserved for reply',
    ]);
  });

  it('omits empty rows rather than showing zeros', () => {
    const view = meterView(usage({ tokens_used: 500 }), '');
    expect(view.breakdown.some((r) => r.label === 'Your draft')).toBe(false);
  });

  it('drops after a compaction reduces the server total', () => {
    const before = meterView(usage({ tokens_used: 9000 }), '');
    const after = meterView(usage({ tokens_used: 1200 }), '');
    expect(before.state).toBe('high');
    expect(after.state).toBe('calm');
    expect(after.percent).toBeLessThan(before.percent);
  });
});

describe('per-session values', () => {
  it('two sessions with different usage produce different meters', () => {
    const a = meterView(usage({ tokens_used: 9200 }), '');
    const b = meterView(usage({ tokens_used: 800 }), '');
    expect(a.percent).not.toBe(b.percent);
    expect(a.state).toBe('high');
    expect(b.state).toBe('calm');
  });
});

describe('latestUsage — per-session value read from the thread itself', () => {
  const msg = (over: Partial<ChatMessage> = {}): ChatMessage => ({
    id: Math.random().toString(36).slice(2),
    role: 'assistant',
    content: 'a',
    createdAt: 1,
    ...over,
  });

  it('reads the most recent reply that carried a reading', () => {
    const messages = [
      msg({ meta: { route: 'chat', context: usage({ tokens_used: 100 }) } }),
      msg({ role: 'user', content: 'q' }),
      msg({ meta: { route: 'chat', context: usage({ tokens_used: 900 }) } }),
    ];
    expect(latestUsage(messages)?.tokens_used).toBe(900);
  });

  it('falls back to an earlier reading while the newest reply is still streaming', () => {
    const messages = [
      msg({ meta: { route: 'chat', context: usage({ tokens_used: 700 }) } }),
      msg({ status: 'streaming', content: '' }), // no meta yet
    ];
    expect(latestUsage(messages)?.tokens_used).toBe(700);
  });

  it('is null for a conversation that has never been answered', () => {
    expect(latestUsage([])).toBeNull();
    expect(latestUsage([msg({ role: 'user', content: 'q' })])).toBeNull();
  });

  it('gives two sessions their own values from their own threads', () => {
    const busy = [msg({ meta: { route: 'chat', context: usage({ tokens_used: 9200 }) } })];
    const quiet = [msg({ meta: { route: 'chat', context: usage({ tokens_used: 400 }) } })];
    expect(meterView(latestUsage(busy), '').state).toBe('high');
    expect(meterView(latestUsage(quiet), '').state).toBe('calm');
  });
});

describe('the tooltip total must agree with the ring', () => {
  it('does not count the reply reservation twice', () => {
    // usable = window − reserved − margin, so the reservation is ALREADY out
    // of the denominator. Adding it to the numerator made the tooltip read
    // 16,747 while the ring beside it read 3% of the same conversation.
    const view = meterView(usage({ tokens_used: 8555 }), '');
    expect(breakdownTotal(view.breakdown)).toBe(8555);
  });

  it('still lists the reservation so the smaller budget is explained', () => {
    const view = meterView(usage({ tokens_used: 100 }), '');
    const held = view.breakdown.find((r) => r.label === 'Reserved for reply');
    expect(held).toBeDefined();
    expect(held?.heldBack).toBe(true);
  });

  it('the total and the ring describe the same number', () => {
    const view = meterView(usage({ tokens_used: 8555 }), '');
    const fromTotal = breakdownTotal(view.breakdown) / view.usableBudget;
    expect(Math.round(fromTotal * 100)).toBe(view.percent);
  });
});

describe('readFoldableCounts — trusting only what the server actually said', () => {
  it('reads the two additive fields off the summary payload', () => {
    expect(
      readFoldableCounts({
        summary: 'older stuff',
        covers_through: 4,
        foldable_turns: 12,
        total_turns: 17,
      }),
    ).toEqual({ foldableTurns: 12, totalTurns: 17 });
  });

  it('reads the same fields off the compact response', () => {
    expect(
      readFoldableCounts({
        compacted: true,
        folded_turns: 27,
        covers_through: 27,
        foldable_turns: 0,
        total_turns: 28,
      }),
    ).toEqual({ foldableTurns: 0, totalTurns: 28 });
  });

  it('is UNKNOWN, not zero, when the fields are missing', () => {
    // An older orchestrator, MOCK_MODE, or a 502 body. Zero would disable a
    // button that still works.
    expect(readFoldableCounts({ summary: null, covers_through: 0 })).toBeNull();
    expect(readFoldableCounts({ compacted: false, reason: 'mock mode' })).toBeNull();
    expect(readFoldableCounts(null)).toBeNull();
    expect(readFoldableCounts('nope')).toBeNull();
  });

  it('rejects values that cannot be counts', () => {
    expect(readFoldableCounts({ foldable_turns: '3', total_turns: 5 })).toBeNull();
    expect(readFoldableCounts({ foldable_turns: -1, total_turns: 5 })).toBeNull();
    expect(readFoldableCounts({ foldable_turns: Number.NaN, total_turns: 5 })).toBeNull();
    expect(readFoldableCounts({ foldable_turns: 3 })).toBeNull();
  });
});

describe('compactPlan — the button may only promise what it will do', () => {
  it('goes dead WITH A REASON when nothing is foldable', () => {
    const plan = compactPlan({ foldable: 0 });
    expect(plan.disabled).toBe(true);
    expect(plan.hint).toBe(NOTHING_TO_COMPACT);
    expect(plan.hint).toContain('Nothing to compact yet');
    expect(plan.hint).toContain('folded automatically as the window fills');
  });

  it('says how much it will fold when there is something', () => {
    const plan = compactPlan({ foldable: 12 });
    expect(plan.disabled).toBe(false);
    expect(plan.label).toBe('Compact now');
    expect(plan.hint).toBe('Folds 12 earlier messages into a summary.');
  });

  it('does not say "1 messages"', () => {
    expect(compactPlan({ foldable: 1 }).hint).toBe(
      'Folds 1 earlier message into a summary.',
    );
    expect(earlierMessages(1)).toBe('1 earlier message');
    expect(earlierMessages(2)).toBe('2 earlier messages');
  });

  it('stays enabled and silent while the count is UNKNOWN', () => {
    // Requirement 3: an unreachable orchestrator must not brick the control.
    const plan = compactPlan({ foldable: null });
    expect(plan.disabled).toBe(false);
    expect(plan.hint).toBeNull();
    expect(plan.label).toBe('Compact now');
  });

  it('is disabled while a compaction is in flight, spinner or not', () => {
    const plan = compactPlan({ foldable: 12, compacting: true });
    expect(plan.disabled).toBe(true);
    expect(plan.label).toBe('Compacting…');
  });

  it('honours the host\'s own veto (no chat open, or streaming)', () => {
    expect(compactPlan({ foldable: 12, blocked: true }).disabled).toBe(true);
    expect(compactPlan({ foldable: null, blocked: true }).disabled).toBe(true);
  });

  it('says nothing lasting until a compaction has actually succeeded', () => {
    const plan = compactPlan({ foldable: 12 });
    expect(plan.folded).toBeNull();
    expect(plan.showSummaryLink).toBe(false);
  });

  it('keeps a lasting line after a compaction, with the way back in', () => {
    // A toast disappears; this is what the popover still says afterwards.
    const plan = compactPlan({ foldable: 0, lastFolded: 12 });
    expect(plan.folded).toBe('Compacted 12 earlier messages');
    expect(plan.showSummaryLink).toBe(true);
    expect(plan.summaryLabel).toBe('See what was kept');
    // And the button beneath it is now honestly dead.
    expect(plan.disabled).toBe(true);
    expect(plan.hint).toBe(NOTHING_TO_COMPACT);
  });

  it('singularises the lasting line too', () => {
    expect(compactPlan({ foldable: 0, lastFolded: 1 }).folded).toBe(
      'Compacted 1 earlier message',
    );
  });

  it('shows no lasting line for a compaction that folded nothing', () => {
    expect(compactPlan({ foldable: 0, lastFolded: 0 }).folded).toBeNull();
  });

  it('the 28-message conversation from the live check, before and after', () => {
    // Server returned folded_turns 27 / covers_through 27 on 28 messages, and
    // "nothing older to summarize" on the second press.
    const before = compactPlan({ foldable: 27 });
    expect(before.disabled).toBe(false);
    expect(before.hint).toBe('Folds 27 earlier messages into a summary.');
    const after = compactPlan({ foldable: 0, lastFolded: 27 });
    expect(after.disabled).toBe(true);
    expect(after.folded).toBe('Compacted 27 earlier messages');
  });
});
