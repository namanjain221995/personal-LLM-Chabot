// @vitest-environment jsdom
/**
 * The Members table: rows render with role/status chips from the live
 * payload, the header line carries the workspace counts, and the list
 * request goes out with the documented query parameters.
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

import AdminMembersPage from '@/app/admin/members/page';
import { AdminMeProvider } from '@/components/admin/AdminMeContext';
import type { Me } from '@/components/admin/api';

const ME: Me = {
  user: { id: 1, name: 'Grace Hopper', email: 'grace@corp.com' },
  workspace: { id: 'w1', name: 'Corp Workspace', role: 'super_admin' },
  capabilities: [
    'workspace.read',
    'members.read',
    'members.manage',
    'roles.manage',
    'invites.manage',
    'workspace_content.read',
    'sessions.manage',
    'audit.read',
  ],
  features: {},
};

const MEMBERS = {
  members: [
    {
      id: 1,
      name: 'Grace Hopper',
      email: 'grace@corp.com',
      role: 'super_admin',
      status: 'active',
      joined_at: '2026-08-01T09:00:00Z',
      last_active_at: '2026-08-31T18:00:00Z',
    },
    {
      id: 2,
      name: 'Ada Lovelace',
      email: 'ada@corp.com',
      role: 'admin',
      status: 'active',
      joined_at: '2026-08-02T09:00:00Z',
      last_active_at: null,
    },
    {
      id: 3,
      name: 'Alan Turing',
      email: 'alan@corp.com',
      role: 'member',
      status: 'disabled',
      joined_at: '2026-08-03T09:00:00Z',
      last_active_at: '2026-08-20T12:00:00Z',
    },
  ],
  total: 3,
  active_members: 2,
  pending_invites: 1,
};

let requested: string[] = [];

function serve() {
  requested = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requested.push(url);
      if (url.startsWith('/api/admin/members?')) {
        return { ok: true, status: 200, json: async () => MEMBERS };
      }
      throw new Error(`unexpected fetch: ${url}`);
    }),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mount() {
  render(
    <AdminMeProvider me={ME}>
      <AdminMembersPage />
    </AdminMeProvider>,
  );
}

describe('the Members table', () => {
  it('requests the documented query parameters', async () => {
    serve();
    mount();
    await waitFor(() => expect(requested.length).toBeGreaterThan(0));
    const url = new URL(requested[0], 'http://localhost');
    expect(url.pathname).toBe('/api/admin/members');
    expect(url.searchParams.get('q')).toBe('');
    expect(url.searchParams.get('role')).toBe('');
    expect(url.searchParams.get('status')).toBe('');
    expect(url.searchParams.get('limit')).toBe('25');
    expect(url.searchParams.get('offset')).toBe('0');
  });

  it('renders every member with name, email and chips', async () => {
    serve();
    mount();
    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeTruthy());
    const table = within(screen.getByRole('table'));
    expect(table.getByText('grace@corp.com')).toBeTruthy();
    expect(table.getByText('ada@corp.com')).toBeTruthy();
    expect(table.getByText('alan@corp.com')).toBeTruthy();
    // Role chips use the readable labels, not the raw enum values.
    expect(table.getByText('Super admin')).toBeTruthy();
    expect(table.getByText('Admin')).toBeTruthy();
    expect(table.getByText('Member')).toBeTruthy();
    // Status chips.
    expect(table.getAllByText('Active')).toHaveLength(2);
    expect(table.getByText('Disabled')).toBeTruthy();
    // Each row has a capability-gated action menu.
    expect(table.getByLabelText('Actions for Alan Turing')).toBeTruthy();
  });

  it('says how many members and pending invites the workspace has', async () => {
    serve();
    mount();
    await waitFor(() =>
      expect(screen.getByText(/2 members · 1 pending invite/)).toBeTruthy(),
    );
  });

  it('offers the invite button to someone with invites.manage', async () => {
    serve();
    mount();
    await waitFor(() => expect(screen.getByText('Invite member')).toBeTruthy());
  });

  it('hides invite and management affordances without the capabilities', async () => {
    serve();
    render(
      <AdminMeProvider
        me={{
          ...ME,
          workspace: { ...ME.workspace, role: 'admin' },
          capabilities: ['members.read'],
        }}
      >
        <AdminMembersPage />
      </AdminMeProvider>,
    );
    await waitFor(() => expect(screen.getByText('Ada Lovelace')).toBeTruthy());
    expect(screen.queryByText('Invite member')).toBeNull();
    // The row menu still exists (View), but shows no management items.
    const table = within(screen.getByRole('table'));
    const trigger = table.getByLabelText('Actions for Alan Turing');
    trigger.click();
    await waitFor(() => expect(screen.getByText('View')).toBeTruthy());
    expect(screen.queryByText('Change role')).toBeNull();
    expect(screen.queryByText('Remove')).toBeNull();
  });
});
