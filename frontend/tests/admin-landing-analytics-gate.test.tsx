// @vitest-environment jsdom
/**
 * The /admin landing page and the analytics capability (2026-09-13).
 *
 * The admin hardening wave (AUDIT F078 / N006) moved per-person usage and
 * its CSV export from WORKSPACE_READ to ANALYTICS_READ, which rbac.py gives
 * to super admins alone. The landing page kept asking for it regardless, so
 * every plain ADMIN opened /admin onto "The analytics could not be loaded."
 * An admin without analytics.read must cause no analytics request, see no
 * export link and see no error; a super admin's page must be unchanged.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AdminAnalyticsPage from '@/app/admin/page';
import { AdminMeProvider } from '@/components/admin/AdminMeContext';
import type { Me } from '@/components/admin/api';

const ADMIN: Me = {
  user: { id: 2, name: 'Grace', email: 'grace@x.test' },
  workspace: { id: 'w', name: 'Acme HQ', role: 'admin' },
  capabilities: ['workspace.read', 'members.read', 'members.manage'],
  features: {},
};

const SUPER_ADMIN: Me = {
  user: { id: 1, name: 'Root', email: 'root@x.test' },
  workspace: { id: 'w', name: 'Acme HQ', role: 'super_admin' },
  capabilities: ['workspace.read', 'members.read', 'analytics.read'],
  features: {},
};

const PAYLOAD = {
  workspace: { id: 'w', name: 'Acme HQ' },
  range: { key: '1m', days: 30, since: '', until: '' },
  summary: {
    members: 3,
    pending_invites: 1,
    active_users: 1,
    messages: 12,
    tool_runs: 4,
  },
  tools: [{ id: 'web_search', label: 'Web search', count: 3 }],
  daily: [{ day: '2026-09-01', messages: 12, active_users: 1 }],
  routes: [],
  members: [],
};

let requested: string[] = [];

beforeEach(() => {
  requested = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      requested.push(String(url));
      // A working server: the admin case must stay quiet because the page
      // does not ask, not because the answer happened to fail.
      if (String(url).includes('analytics')) {
        return {
          ok: true,
          status: 200,
          json: async () => PAYLOAD,
        } as unknown as Response;
      }
      return {
        ok: false,
        status: 404,
        json: async () => ({ detail: 'Not found' }),
      } as unknown as Response;
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount(me: Me) {
  return render(
    <AdminMeProvider me={me}>
      <AdminAnalyticsPage />
    </AdminMeProvider>,
  );
}

describe('the admin landing page without analytics.read', () => {
  it('never asks the server for the per-person analytics', async () => {
    mount(ADMIN);
    await screen.findByText('Acme HQ');
    // Give any effect a chance to fire before asserting it did not.
    await new Promise((r) => setTimeout(r, 20));
    expect(requested.filter((u) => u.includes('analytics'))).toEqual([]);
  });

  it('does not link the CSV export', async () => {
    mount(ADMIN);
    await screen.findByText('Acme HQ');
    expect(screen.queryByText('Export')).toBeNull();
    expect(
      document.querySelector('a[href*="/api/admin/analytics/export"]'),
    ).toBeNull();
  });

  it('shows no error and no analytics sections', async () => {
    mount(ADMIN);
    await screen.findByText('Acme HQ');
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText('The analytics could not be loaded.')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText('Workspace analytics')).toBeNull();
    expect(screen.queryByRole('group', { name: 'Time range' })).toBeNull();
    expect(screen.queryByText('Tool runs')).toBeNull();
  });
});

describe('the admin landing page with analytics.read', () => {
  it('loads the analytics and shows the headline numbers as before', async () => {
    mount(SUPER_ADMIN);
    await waitFor(() => screen.getByText('Acme HQ · last 30 days'));
    expect(requested.some((u) => u.includes('analytics?range=1m'))).toBe(true);
    expect(screen.getByText('Workspace analytics')).toBeTruthy();
    expect(screen.getByRole('group', { name: 'Time range' })).toBeTruthy();
    expect(screen.getAllByText('12').length).toBeGreaterThan(0);
  });

  it('links the CSV export for the window on screen', async () => {
    mount(SUPER_ADMIN);
    await waitFor(() => screen.getByText('Acme HQ · last 30 days'));
    const link = screen.getByText('Export').closest('a');
    expect(link?.getAttribute('href')).toBe(
      '/api/admin/analytics/export?range=1m',
    );
  });
});
