// @vitest-environment jsdom
/**
 * What the summary panel is allowed to say when there is no summary.
 *
 * It used to say: "Nothing has been compacted yet — the assistant still sees
 * every message in this chat exactly as written."
 *
 * On a live conversation both halves of that were false at the same time. The
 * server HAD compacted (five turns, boundary advanced, ~8,700 tokens dropped
 * from every later prompt) and had stored a zero-length summary against them.
 * The toast said "Compacted 5 earlier messages into the summary"; this panel,
 * one click later, told the user nothing had happened and everything was
 * still there. The second claim is also unknowable from the browser: what the
 * model can still see is not something the client can observe.
 *
 * The only fact available here is whether a summary came back. That is now the
 * only thing said.
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SummaryPanel } from '@/components/SummaryPanel';
import { NO_USABLE_SUMMARY } from '@/lib/compact';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function serve(body: unknown, ok = true) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok, status: ok ? 200 : 500, json: async () => body })),
  );
}

function open() {
  render(<SummaryPanel conversationId="conv-1" open onClose={() => undefined} />);
}

/** Language the panel must never use again, in any state. */
const FORBIDDEN = [
  /still sees every message/i,
  /exactly as written/i,
  /nothing has been compacted/i,
];

function assertNoOverclaiming() {
  const text = document.body.textContent ?? '';
  for (const pattern of FORBIDDEN) {
    expect(text).not.toMatch(pattern);
  }
}

describe('E — no usable summary', () => {
  beforeEach(() => {
    // The exact shape the live defect produced: compaction reported, summary
    // stored empty.
    serve({ summary: '', covers_through: 5, foldable_turns: 0 });
  });

  it('states only that no summary is available', async () => {
    open();
    await waitFor(() => screen.getByText(NO_USABLE_SUMMARY));
    assertNoOverclaiming();
  });

  it('does not claim older messages "were summarized" either', async () => {
    open();
    await waitFor(() => screen.getByText(NO_USABLE_SUMMARY));
    // The footnote describes a summary; with none there is nothing to footnote.
    expect(document.body.textContent).not.toMatch(/were summarized/i);
  });
});

describe('E — a whitespace-only summary is no summary', () => {
  it('falls into the same neutral state', async () => {
    serve({ summary: '\n \t ' });
    open();
    await waitFor(() => screen.getByText(NO_USABLE_SUMMARY));
    assertNoOverclaiming();
  });
});

describe('E — a real summary still renders as before', () => {
  it('shows the text and the read-only footnote', async () => {
    serve({ summary: '## Notes\n- ATM system requested', covers_through: 1 });
    open();
    await waitFor(() => screen.getByText(/ATM system requested/));
    expect(screen.getByText(/it is read-only/i)).toBeTruthy();
    expect(screen.queryByText(NO_USABLE_SUMMARY)).toBeNull();
    assertNoOverclaiming();
  });
});

describe('E — a failed read says so, and claims nothing', () => {
  it('shows the existing error line only', async () => {
    serve({}, false);
    open();
    await waitFor(() => screen.getByText(/load the summary for this/i));
    expect(screen.queryByText(NO_USABLE_SUMMARY)).toBeNull();
    assertNoOverclaiming();
  });
});
