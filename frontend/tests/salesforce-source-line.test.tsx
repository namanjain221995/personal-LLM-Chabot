// @vitest-environment jsdom
/**
 * Where an answer's numbers came from, under the answer.
 *
 * The source, the scope and the assumptions render on their own, because the
 * warehouse route is what every Salesforce answer now reads and it has no `live`
 * block to hang them from. What must not change is an OLD answer: until this
 * change `meta.assumptions` held whatever the planner assumed, whether or not the
 * statement did it, and with no source line it was never on screen.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { MessageRow } from '@/components/MessageRow';
import type { ChatMessage, Meta } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ASSUMED = 'No date filter was applied, so this covers all dates.';
const SYNCED = {
  source: 'synced',
  objects: ['Interview__c'],
  query_timestamp: '2026-11-05T09:00:00+00:00',
};

function row(meta: Meta) {
  const message = {
    id: 'm1',
    role: 'assistant',
    content: 'There are 84.',
    meta,
  } as unknown as ChatMessage;
  return render(
    <MessageRow message={message} isLast onRegenerate={vi.fn()} onRetry={vi.fn()} />,
  );
}

describe('the source line', () => {
  it('names the synced copy, the object and when the copy was published', () => {
    row({ route: 'sql', salesforce_sources: SYNCED } as Meta);
    const line = screen.getByText(/synced copy/);
    expect(line.textContent).toContain('Interview__c');
    expect(line.textContent).toContain('2026');
    expect(line.textContent).not.toContain('live from Salesforce');
  });

  it('shows no record count for a result of figures', () => {
    row({ route: 'sql', salesforce_sources: SYNCED } as Meta);
    expect(screen.getByText(/synced copy/).textContent).not.toMatch(/record/);
  });

  it('shows the assumption the engine checked', () => {
    row({
      route: 'sql',
      salesforce_sources: SYNCED,
      assumptions: [ASSUMED],
      assumptions_applied: true,
    } as Meta);
    expect(screen.getByText(ASSUMED)).toBeTruthy();
  });

  it('shows a checked assumption with no source line beside it', () => {
    row({ route: 'sql', assumptions: [ASSUMED], assumptions_applied: true } as Meta);
    expect(screen.getByText(ASSUMED)).toBeTruthy();
    expect(screen.queryByText(/synced copy/)).toBeNull();
  });

  it('shows a scope on its own too', () => {
    row({
      route: 'sql',
      salesforce_scope: 'Interview__c',
      assumptions_applied: true,
    } as Meta);
    expect(screen.getByText('Scope:')).toBeTruthy();
  });

  it('does not start showing what an old answer only assumed', () => {
    row({
      route: 'sql',
      assumptions: ['Assumed date range: this quarter.'],
      salesforce_scope: 'date range: this quarter',
    } as Meta);
    expect(screen.queryByText(/this quarter/)).toBeNull();
    expect(screen.queryByText('Scope:')).toBeNull();
  });

  it('keeps an old live answer as it was', () => {
    row({
      route: 'sql',
      salesforce_sources: { source: 'live', objects: ['Lead'] },
      assumptions: ['Assumed date range: this quarter.'],
    } as Meta);
    expect(screen.getByText(/live from Salesforce/)).toBeTruthy();
    expect(screen.getByText('Assumed date range: this quarter.')).toBeTruthy();
  });
});
