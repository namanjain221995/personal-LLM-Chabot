// @vitest-environment jsdom
/**
 * The member page under the inspect rule (rbac.may_inspect, owner decision
 * 2026-09-14: yourself; a super admin reads everyone, other super admins
 * included; anyone else only a lower role).
 *
 * When GET members/{id} says `may_inspect: false` (an admin opening a super
 * admin or a peer admin) the page must not call the refused routes at all,
 * the stat tiles read "Private" with an accessible label, and every tab shows
 * one calm notice — never the red "No such member." box with a Retry. When it
 * is true nothing changes, and a genuine 404 still shows the error.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import type { ComponentProps, ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/navigation', () => ({
  useParams: () => ({ id: '9' }),
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock('next/link', () => ({
  __esModule: true,
  default: (props: ComponentProps<'a'> & { children?: ReactNode }) => {
    const { children, ...rest } = props;
    return <a {...rest}>{children}</a>;
  },
}));

vi.mock('@/components/Providers', () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

import AdminMemberDetailPage from '@/app/admin/members/[id]/page';
import { AdminMeProvider } from '@/components/admin/AdminMeContext';
import { mayInspectMember, type Me } from '@/components/admin/api';

const ADMIN_ME: Me = {
  user: { id: 2, name: 'Fixture Admin', email: 'admin@example.test' },
  workspace: { id: 'w1', name: 'Fixture Workspace', role: 'admin' },
  capabilities: [
    'workspace.read',
    'members.read',
    'members.manage',
    'invites.manage',
    'workspace_content.read',
    'sessions.manage',
  ],
  features: {},
};

const TARGET = {
  id: 9,
  name: 'Fixture Target',
  email: 'target@example.test',
  role: 'super_admin',
  status: 'active',
  joined_at: '2026-08-01T09:00:00Z',
  last_active_at: '2026-09-13T18:00:00Z',
};

const STATS = {
  conversations: 12,
  messages: 340,
  uploads: 3,
  reports: 2,
  memory_facts: 5,
  research_runs: 1,
};

const PRIVATE =
  "This member's conversations, uploads, reports and sessions are private to higher roles.";

type Reply = { status: number; body: unknown };

function routeFetch(routes: Record<string, Reply>) {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      const reply = routes[url.split('?')[0]] ?? {
        status: 500,
        body: { detail: `unexpected ${url}` },
      };
      return {
        ok: reply.status >= 200 && reply.status < 300,
        status: reply.status,
        json: async () => reply.body,
      };
    }),
  );
  return calls;
}

function renderPage() {
  return render(
    <AdminMeProvider me={ADMIN_ME}>
      <AdminMemberDetailPage />
    </AdminMeProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('member page, may_inspect false', () => {
  it('calls only the member detail, labels the tiles private and shows the notice on every tab', async () => {
    const calls = routeFetch({
      '/api/admin/members/9': {
        status: 200,
        body: { member: TARGET, stats: null, may_inspect: false },
      },
    });
    renderPage();

    await waitFor(() => expect(screen.getByText(PRIVATE)).toBeTruthy());
    // The header still shows the member row.
    expect(screen.getByText('Fixture Target')).toBeTruthy();

    // Six tiles, each "Private" with an accessible label; no dashes.
    for (const label of [
      'Conversations',
      'Messages',
      'Uploads',
      'Reports',
      'Memory facts',
      'Research runs',
    ]) {
      expect(screen.getByRole('group', { name: `${label}: private` })).toBeTruthy();
    }
    expect(screen.getAllByText('Private')).toHaveLength(6);
    expect(screen.queryByText('—')).toBeNull();

    // No red failure, no Retry that can never succeed.
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(screen.queryByText('No such member.')).toBeNull();

    // Tabs stay visible, each with the same notice.
    const tabs = screen.getAllByRole('tab').map((t) => t.textContent);
    expect(tabs).toEqual(['Conversations', 'Uploads', 'Reports', 'Sessions']);
    for (const name of ['Uploads', 'Reports', 'Sessions', 'Conversations']) {
      fireEvent.click(screen.getByRole('tab', { name }));
      expect(screen.getByRole('status').textContent).toBe(PRIVATE);
      expect(screen.queryByRole('alert')).toBeNull();
    }

    // Let any stray effect flush, then: nothing but the detail was requested.
    await new Promise((r) => setTimeout(r, 20));
    expect(calls).toEqual(['/api/admin/members/9']);
  });

  it('treats an older orchestrator (no may_inspect, stats null) the same way', () => {
    expect(mayInspectMember({ stats: null })).toBe(false);
    expect(mayInspectMember({ stats: STATS })).toBe(true);
    expect(mayInspectMember({ stats: null, may_inspect: true })).toBe(true);
    expect(mayInspectMember({ stats: STATS, may_inspect: false })).toBe(false);
  });
});

describe('member page, may_inspect true', () => {
  it('renders the counts and loads the conversations tab as before', async () => {
    const calls = routeFetch({
      '/api/admin/members/9': {
        status: 200,
        body: { member: TARGET, stats: STATS, may_inspect: true },
      },
      '/api/admin/members/9/conversations': {
        status: 200,
        body: {
          conversations: [
            {
              id: 'c-1',
              title: 'Fixture conversation',
              updated_at: '2026-09-13T10:00:00Z',
              message_count: 4,
            },
          ],
          total: 1,
        },
      },
    });
    renderPage();

    await waitFor(() =>
      expect(screen.getByText('Fixture conversation')).toBeTruthy(),
    );
    expect(screen.getByText('340')).toBeTruthy();
    expect(screen.queryByText('Private')).toBeNull();
    expect(screen.queryByText(PRIVATE)).toBeNull();
    expect(calls[0]).toBe('/api/admin/members/9');
    expect(calls).toContain('/api/admin/members/9/conversations?limit=25&offset=0');
  });

  it('still shows the error with Retry on a genuine 404 from a tab', async () => {
    routeFetch({
      '/api/admin/members/9': {
        status: 200,
        body: { member: TARGET, stats: STATS, may_inspect: true },
      },
      '/api/admin/members/9/conversations': {
        status: 404,
        body: { detail: 'No such member.' },
      },
    });
    renderPage();

    await waitFor(() => expect(screen.getByText('No such member.')).toBeTruthy());
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(screen.queryByText(PRIVATE)).toBeNull();
  });

  it('still shows the error when the member itself is gone', async () => {
    const calls = routeFetch({
      '/api/admin/members/9': { status: 404, body: { detail: 'No such member.' } },
    });
    renderPage();

    await waitFor(() => expect(screen.getByText('No such member.')).toBeTruthy());
    expect(screen.getByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    expect(calls).toEqual(['/api/admin/members/9']);
  });
});
