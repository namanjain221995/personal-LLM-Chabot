/**
 * "Compact now" — what the browser is allowed to believe, and what the meter
 * is allowed to show afterwards.
 *
 * Two production defects are pinned here.
 *
 * 1. The ring was forced to zero the moment the compact request returned, on
 *    the assumption that the next prompt had to be smaller. Measured against
 *    the running orchestrator it often is not: on a real conversation the
 *    model's input went 1,986 → 2,159 tokens ACROSS a compaction. So the user
 *    watched 25% → 0% → 31%, and the only honest reading in that sequence was
 *    the one the UI replaced.
 *
 * 2. `POST /chat/compact` answers `{compacted: true, folded_turns: 5}` as soon
 *    as it advances its fold boundary — including when the summary it stored
 *    was the empty string. The UI reported "Compacted 5 earlier messages into
 *    the summary" and offered a link to read it; the panel behind that link
 *    then said nothing had been compacted at all. Both could not be true, and
 *    the one the browser could actually check was the summary.
 *
 * The browser cannot repair either. It can refuse to assert them.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  COMPACT_FAILED,
  COMPACT_UNVERIFIED,
  NOTHING_FOLDED,
  compactOutcome,
  isCompacting,
  requestCompact,
  usableSummary,
} from '../lib/compact';
import { latestUsage, meterView } from '../lib/contextMeter';
import type { ChatMessage, ContextUsage } from '../lib/types';

/* ------------------------------------------------------------- fixtures */

const usage = (tokens: number): ContextUsage => ({
  tokens_used: tokens,
  usable_budget: 10000,
  window: 131072,
  reserved_output: 8192,
  fraction: tokens / 10000,
  summarized_turns: 0,
});

const reply = (id: string, tokens: number): ChatMessage => ({
  id,
  role: 'assistant',
  content: 'ok',
  createdAt: 0,
  meta: { route: 'chat', context: usage(tokens) },
});

const ask = (id: string): ChatMessage => ({
  id,
  role: 'user',
  content: 'hello',
  createdAt: 0,
});

const json = (body: unknown, ok = true) => ({
  ok,
  status: ok ? 200 : 500,
  json: async () => body,
});

let calls: string[] = [];

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ------------------------------------------------- A. meter honesty */

describe('A — the meter keeps the last measurement the SERVER made', () => {
  // The meter's only inputs are the newest usage on the thread and the live
  // draft. Compaction appends no message, so by construction it cannot move
  // the ring: there is no longer any code path that can.
  it('still reads X immediately after a compaction, not 0', () => {
    const before: ChatMessage[] = [ask('u1'), reply('a1', 2500)];
    expect(meterView(latestUsage(before), '').percent).toBe(25);

    // A compaction changes no message — this IS the post-compact thread.
    const afterCompact = [...before];
    expect(meterView(latestUsage(afterCompact), '').percent).toBe(25);
  });

  it('adopts the next server measurement, whichever way it moved', () => {
    const lower = [ask('u1'), reply('a1', 2500), ask('u2'), reply('a2', 1800)];
    expect(meterView(latestUsage(lower), '').percent).toBe(18);

    // Compaction made the prompt BIGGER — measured, not hypothetical. The
    // meter must report that too rather than flattering the button.
    const higher = [ask('u1'), reply('a1', 2500), ask('u2'), reply('a2', 3100)];
    expect(meterView(latestUsage(higher), '').percent).toBe(31);
  });

  it('never invents a budget or a saving of its own', () => {
    const thread = [ask('u1'), reply('a1', 2500)];
    const view = meterView(latestUsage(thread), '');
    expect(view.tokensUsed).toBe(2500); // exactly what the server said
    expect(view.usableBudget).toBe(10000);
  });
});

/* ------------------------------------------- B. one press, one request */

describe('B — a second press while one is in flight is not a second request', () => {
  it('suppresses the duplicate and reports it as "already running"', async () => {
    let release!: (v: unknown) => void;
    const gate = new Promise((r) => {
      release = r;
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        calls.push(url);
        if (url === '/api/chat/compact') {
          await gate;
          return json({ compacted: false, foldable_turns: 0, total_turns: 1 });
        }
        return json({ summary: 'notes' });
      }),
    );

    const first = requestCompact('conv-1', [{ role: 'user', content: 'hi' }]);
    // Synchronously, before the first has resolved:
    expect(isCompacting('conv-1')).toBe(true);
    const second = await requestCompact('conv-1', [
      { role: 'user', content: 'hi' },
    ]);
    expect(second).toBeNull(); // null = "nothing happened", NOT an error

    release(null);
    await first;
    expect(calls.filter((c) => c === '/api/chat/compact')).toHaveLength(1);
    // …and the guard clears, so the control is usable again.
    expect(isCompacting('conv-1')).toBe(false);
  });

  it('clears the guard even when the request throws', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('network down');
      }),
    );
    const run = await requestCompact('conv-2', []);
    expect(run?.outcome.kind).toBe('failed');
    expect(isCompacting('conv-2')).toBe(false);
  });
});

/* ------------------------- B2. the transcript is never a casualty (CV-08) */

describe('B2 — a failed or unverified compaction leaves the messages alone', () => {
  it.each([
    [
      'unverified',
      async (url: string) =>
        url === '/api/chat/compact'
          ? json({ compacted: true, folded_turns: 4, foldable_turns: 0, total_turns: 5 })
          : json({ summary: '   ' }),
    ],
    [
      'failed',
      async () => {
        throw new Error('network down');
      },
    ],
  ])('CV-08 does not touch the thread it was handed (%s)', async (_kind, impl) => {
    vi.stubGlobal('fetch', vi.fn(impl as never));
    const messages = [
      { role: 'user', content: 'My customer is Acme.' },
      { role: 'assistant', content: 'Understood.' },
    ];
    const snapshot = JSON.stringify(messages);

    const run = await requestCompact('conv-untouched', messages);

    expect(run?.outcome.kind).not.toBe('compacted');
    // The browser cannot repair the server's state; the least it can do is
    // not damage what is on screen while reporting that.
    expect(JSON.stringify(messages)).toBe(snapshot);
    expect(messages).toHaveLength(2);
  });
});

/* ------------------------------- C. success the browser cannot confirm */

describe('C — a compaction with no summary behind it is NOT a success', () => {
  it('reports unverified when the stored summary is empty', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        calls.push(url);
        if (url === '/api/chat/compact') {
          return json({ compacted: true, folded_turns: 5, foldable_turns: 0 });
        }
        return json({ summary: '', foldable_turns: 0, total_turns: 6 });
      }),
    );

    const run = await requestCompact('conv-3', [
      { role: 'user', content: 'hi' },
    ]);
    expect(run?.outcome.kind).toBe('unverified');
    expect(run?.outcome.tone).toBe('error');
    expect(run?.outcome.message).toBe(COMPACT_UNVERIFIED);
    // The claim was checked against the endpoint that already exists.
    expect(calls[1]).toContain('/api/history/conversations/conv-3/summary');
  });

  it('treats a whitespace-only summary as no summary', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url === '/api/chat/compact'
          ? json({ compacted: true, folded_turns: 2 })
          : json({ summary: '\n   \t ' }),
      ),
    );
    const run = await requestCompact('conv-4', []);
    expect(run?.outcome.kind).toBe('unverified');
  });

  it('treats "could not check" as unverified, never as success', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/chat/compact') {
          return json({ compacted: true, folded_turns: 3 });
        }
        throw new Error('summary endpoint unreachable');
      }),
    );
    const run = await requestCompact('conv-5', []);
    expect(run?.outcome.kind).toBe('unverified');
  });

  it('never carries a folded count on an unverified result', async () => {
    // The count is what the old success sentence was built from; an
    // unverified outcome must not expose one at all.
    const outcome = compactOutcome({
      compacted: true,
      foldedTurns: 5,
      summaryVerified: false,
    });
    expect(outcome.kind).toBe('unverified');
    expect(outcome).not.toHaveProperty('foldedTurns');
  });
});

/* ------------------------------------------ D. a genuinely good result */

describe('D — a verified compaction reports success and refreshes state', () => {
  it('confirms the summary, then claims the fold', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) =>
        url === '/api/chat/compact'
          ? json({ compacted: true, folded_turns: 12, foldable_turns: 4 })
          : json({
              summary: '## Notes\n- ATM system requested',
              foldable_turns: 0,
              total_turns: 13,
            }),
      ),
    );

    const run = await requestCompact('conv-6', []);
    expect(run?.outcome).toMatchObject({
      kind: 'compacted',
      foldedTurns: 12,
      tone: 'info',
      message: 'Compacted 12 earlier messages into the summary.',
    });
    // Reconciled from the SUMMARY read, which is newer than the POST body.
    expect(run?.foldableTurns).toBe(0);
  });

  it('says "1 earlier message" for a single turn', () => {
    expect(
      compactOutcome({
        compacted: true,
        foldedTurns: 1,
        summaryVerified: true,
      }).message,
    ).toBe('Compacted 1 earlier message into the summary.');
  });

  it('reports "nothing to compact" without a second round trip', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        calls.push(url);
        return json({
          compacted: false,
          reason: 'nothing older to summarize',
          foldable_turns: 0,
          total_turns: 2,
        });
      }),
    );
    const run = await requestCompact('conv-7', []);
    expect(run?.outcome.kind).toBe('nothing');
    // Product copy, NOT the server's internal wording echoed at the user.
    expect(run?.outcome.message).toBe(NOTHING_FOLDED);
    expect(run?.outcome.message).not.toContain('summarize');
    expect(calls).toHaveLength(1);
  });
});

/* --------------------------------------------------- failure surfaces */

describe('a failed request stays friendly and leaks nothing', () => {
  it('does not echo the proxy body on a non-2xx', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        json({ compacted: false, reason: 'orchestrator unreachable' }, false),
      ),
    );
    const run = await requestCompact('conv-8', []);
    expect(run?.outcome.kind).toBe('failed');
    expect(run?.outcome.message).toBe(COMPACT_FAILED);
    expect(run?.outcome.message).not.toContain('orchestrator');
    // Unknown, not zero — a server that could not answer must not disable a
    // control that still works.
    expect(run?.foldableTurns).toBeNull();
  });
});

describe('usableSummary — the one definition of "there is a summary"', () => {
  it.each([
    ['missing', {}],
    ['null', { summary: null }],
    ['empty', { summary: '' }],
    ['whitespace', { summary: '   \n\t' }],
    ['not a string', { summary: 42 }],
    ['not an object', 'nope'],
  ])('%s → null', (_label, body) => {
    expect(usableSummary(body)).toBeNull();
  });

  it('returns the trimmed text when there is real content', () => {
    expect(usableSummary({ summary: '  ## Notes\n- a  ' })).toBe(
      '## Notes\n- a',
    );
  });
});
