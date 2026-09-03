// @vitest-environment jsdom
/**
 * The workspace analytics page and its chart (2026-09-03).
 *
 * What matters here is not the totals — the server computes those — but that
 * the page asks for the window the user picked, that People lists members
 * who used NOTHING (the question this table is usually opened to answer),
 * and that a year of days folds into readable buckets instead of 365
 * hairlines.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AdminAnalyticsPage from '@/app/admin/page';
import { AdminMeProvider } from '@/components/admin/AdminMeContext';
import { bucketize } from '@/components/admin/UsageChart';
import type { Me } from '@/components/admin/api';

const ME: Me = {
  user: { id: 1, name: 'Root', email: 'root@x.test' },
  workspace: { id: 'w', name: 'Acme HQ', role: 'super_admin' },
  capabilities: ['workspace.read', 'members.read'],
  features: {},
};

function payload(range: string) {
  return {
    workspace: { id: 'w', name: 'Acme HQ' },
    range: { key: range, days: range === '7d' ? 7 : 30, since: '', until: '' },
    summary: {
      members: 3,
      pending_invites: 1,
      active_users: 1,
      messages: 12,
      tool_runs: 4,
    },
    tools: [
      { id: 'web_search', label: 'Web search', count: 3 },
      { id: 'deep_research', label: 'Deep research', count: 1 },
    ],
    daily: [
      { day: '2026-09-01', messages: 5, active_users: 1 },
      { day: '2026-09-02', messages: 7, active_users: 1 },
    ],
    routes: [{ route: 'chat', count: 8 }],
    members: [
      {
        id: 2,
        name: 'Bob',
        email: 'bob@x.test',
        role: 'member',
        status: 'active',
        last_active_at: '2026-09-02T10:00:00Z',
        messages: 12,
        answers: 12,
        conversations: 3,
        tool_runs: 4,
        web_search: 3,
        deep_research: 1,
        salesforce: 0,
        files: 0,
        agent: 0,
        links: 0,
      },
      {
        id: 3,
        name: 'Quiet Carol',
        email: 'carol@x.test',
        role: 'member',
        status: 'active',
        last_active_at: null,
        messages: 0,
        answers: 0,
        conversations: 0,
        tool_runs: 0,
        web_search: 0,
        deep_research: 0,
        salesforce: 0,
        files: 0,
        agent: 0,
        links: 0,
      },
    ],
  };
}

let requested: string[] = [];

beforeEach(() => {
  requested = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      requested.push(String(url));
      const range = String(url).includes('range=7d') ? '7d' : '1m';
      return {
        ok: true,
        status: 200,
        json: async () => payload(range),
      } as unknown as Response;
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount() {
  return render(
    <AdminMeProvider me={ME}>
      <AdminAnalyticsPage />
    </AdminMeProvider>,
  );
}

describe('workspace analytics', () => {
  it('loads the default month and shows the headline numbers', async () => {
    mount();
    await waitFor(() => screen.getByText('Acme HQ · last 30 days'));
    expect(requested[0]).toContain('analytics?range=1m');
    expect(screen.getByText('12')).toBeTruthy(); // messages
    expect(screen.getByText('Tool runs')).toBeTruthy();
  });

  it('re-asks the server when the range pill changes', async () => {
    mount();
    await waitFor(() => screen.getByText('Acme HQ · last 30 days'));
    fireEvent.click(screen.getByRole('button', { name: '7D' }));
    await waitFor(() =>
      expect(requested.some((u) => u.includes('range=7d'))).toBe(true),
    );
  });

  it('lists members who used nothing under People', async () => {
    mount();
    await waitFor(() => screen.getByText('Acme HQ · last 30 days'));
    fireEvent.click(screen.getByRole('tab', { name: /People/ }));
    await waitFor(() => screen.getByText('Quiet Carol'));
    expect(screen.getByText('Bob')).toBeTruthy();
  });

  it('exports the window that is on screen', async () => {
    mount();
    await waitFor(() => screen.getByText('Acme HQ · last 30 days'));
    const link = screen.getByText('Export').closest('a');
    expect(link?.getAttribute('href')).toBe(
      '/api/admin/analytics/export?range=1m',
    );
  });
});

describe('the usage chart', () => {
  const days = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      day: `2026-01-${String((i % 28) + 1).padStart(2, '0')}`,
      messages: i,
      active_users: 1,
    }));

  it('keeps a short window daily', () => {
    const bars = bucketize(days(7));
    expect(bars).toHaveLength(7);
    expect(bars.every((b) => b.days === 1)).toBe(true);
  });

  it('folds a long window into buckets rather than hairlines', () => {
    const bars = bucketize(days(365));
    expect(bars.length).toBeLessThanOrEqual(31);
    expect(bars[0].days).toBeGreaterThan(1);
    // Nothing is lost in the fold.
    const total = bars.reduce((n, b) => n + b.messages, 0);
    expect(total).toBe(days(365).reduce((n, d) => n + d.messages, 0));
  });

  it('handles an empty window', () => {
    expect(bucketize([])).toEqual([]);
  });
});
