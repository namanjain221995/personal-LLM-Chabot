// @vitest-environment jsdom
/**
 * The /admin layout's gate. Signed out (401) → HARD redirect to /login;
 * signed in without members.read → back to the chat; with members.read the
 * shell renders — and the Audit Log link exists ONLY with audit.read
 * (capability-driven visibility, never role comparisons).
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps, ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/navigation', () => ({
  usePathname: () => '/admin',
}));

vi.mock('next/link', () => ({
  __esModule: true,
  default: (props: ComponentProps<'a'> & { children?: ReactNode }) => {
    const { children, ...rest } = props;
    return <a {...rest}>{children}</a>;
  },
}));

import AdminLayout from '@/app/admin/layout';
import { nav } from '@/components/admin/nav';

const ME = {
  username: 'grace',
  user: { id: 1, name: 'Grace Hopper', email: 'grace@corp.com' },
  workspace: { id: 'w1', name: 'Corp Workspace', role: 'admin' },
  capabilities: [
    'workspace.read',
    'members.read',
    'members.manage',
    'invites.manage',
    'workspace_content.read',
    'sessions.manage',
  ],
};

function serveMe(status: number, body: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    })),
  );
}

let assign: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  assign = vi.spyOn(nav, 'assign').mockImplementation(() => undefined);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mount() {
  render(
    <AdminLayout>
      <p>admin page content</p>
    </AdminLayout>,
  );
}

describe('the /admin gate', () => {
  it('hard-redirects to /login when signed out', async () => {
    serveMe(401, { detail: 'Sign in required.' });
    mount();
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/login'));
    expect(screen.queryByText('admin page content')).toBeNull();
  });

  it('sends a signed-in non-admin back to the chat', async () => {
    serveMe(200, { ...ME, capabilities: [] });
    mount();
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/'));
    expect(screen.queryByText('admin page content')).toBeNull();
  });

  it('stays put on a network failure instead of redirecting', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('fetch failed');
      }),
    );
    mount();
    await waitFor(() =>
      expect(screen.getByText(/could not be reached/i)).toBeTruthy(),
    );
    expect(assign).not.toHaveBeenCalled();
  });
});

describe('the /admin shell', () => {
  it('renders the nav and the page for an admin, without Audit Log', async () => {
    serveMe(200, ME);
    mount();
    await waitFor(() =>
      expect(screen.getByText('admin page content')).toBeTruthy(),
    );
    expect(assign).not.toHaveBeenCalled();
    // Desktop rail + mobile header both carry the links.
    expect(screen.getAllByText('Overview').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Members').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Invitations').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Back to chat').length).toBeGreaterThan(0);
    // An admin has no audit.read: the link must not exist as a dead entry.
    expect(screen.queryAllByText('Audit Log')).toHaveLength(0);
    expect(document.querySelector('a[href="/admin/audit"]')).toBeNull();
    // Workspace name labels the rail.
    expect(screen.getAllByText('Corp Workspace').length).toBeGreaterThan(0);
  });

  it('shows Audit Log with audit.read', async () => {
    serveMe(200, {
      ...ME,
      workspace: { ...ME.workspace, role: 'super_admin' },
      capabilities: [...ME.capabilities, 'audit.read', 'roles.manage'],
    });
    mount();
    await waitFor(() =>
      expect(screen.getByText('admin page content')).toBeTruthy(),
    );
    expect(screen.getAllByText('Audit Log').length).toBeGreaterThan(0);
    const link = document.querySelector('a[href="/admin/audit"]');
    expect(link).not.toBeNull();
  });
});
