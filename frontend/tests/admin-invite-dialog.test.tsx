// @vitest-environment jsdom
/**
 * The invite dialog: the form POSTs the documented body, and the success view
 * shows the ONE-TIME accept link (full origin + accept_path) with a copy
 * affordance and a "shown once" warning — the token exists nowhere else.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { InviteDialog } from '@/components/admin/InviteDialog';
import { AdminMeProvider } from '@/components/admin/AdminMeContext';
import type { Me } from '@/components/admin/api';

const SUPER: Me = {
  user: { id: 1, name: 'Grace Hopper', email: 'grace@corp.com' },
  workspace: { id: 'w1', name: 'Corp Workspace', role: 'super_admin' },
  capabilities: ['members.read', 'invites.manage', 'roles.manage'],
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mount(me: Me = SUPER) {
  render(
    <AdminMeProvider me={me}>
      <InviteDialog open onClose={() => undefined} />
    </AdminMeProvider>,
  );
}

describe('the invite dialog', () => {
  it('POSTs the invitation and reveals the one-time link', async () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(input), init: init ?? {} });
        return {
          ok: true,
          status: 200,
          json: async () => ({
            id: 'inv-1',
            email: 'ada@corp.com',
            role: 'admin',
            expires_at: '2026-09-08T10:00:00Z',
            accept_path: '/accept-invite?token=tok-123',
          }),
        };
      }),
    );
    mount();

    fireEvent.change(screen.getByLabelText('Name (optional)'), {
      target: { value: 'Ada Lovelace' },
    });
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'ada@corp.com' },
    });
    fireEvent.change(screen.getByLabelText('Role'), {
      target: { value: 'admin' },
    });
    fireEvent.click(screen.getByText('Create invite'));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url).toBe('/api/admin/invitations');
    expect(calls[0].init.method).toBe('POST');
    expect(JSON.parse(String(calls[0].init.body))).toEqual({
      email: 'ada@corp.com',
      name: 'Ada Lovelace',
      role: 'admin',
    });

    // The one-time link: current origin + accept_path, copyable, warned about.
    const link = `${window.location.origin}/accept-invite?token=tok-123`;
    await waitFor(() => expect(screen.getByText(link)).toBeTruthy());
    expect(screen.getByLabelText('Copy link')).toBeTruthy();
    expect(screen.getByText(/shown once/i)).toBeTruthy();
  });

  it('limits the role options to what the inviter may assign', () => {
    vi.stubGlobal('fetch', vi.fn());
    mount({
      ...SUPER,
      workspace: { ...SUPER.workspace, role: 'admin' },
      capabilities: ['members.read', 'invites.manage'],
    });
    const select = screen.getByLabelText('Role') as HTMLSelectElement;
    const options = Array.from(select.options).map((o) => o.value);
    // An admin invites members only; super_admin is never invitable.
    expect(options).toEqual(['member']);
  });

  it('surfaces a 409 (already a member) inline', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 409,
        json: async () => ({ detail: 'That person is already a member.' }),
      })),
    );
    mount();
    fireEvent.change(screen.getByLabelText('Email'), {
      target: { value: 'grace@corp.com' },
    });
    fireEvent.click(screen.getByText('Create invite'));
    await waitFor(() =>
      expect(
        screen.getByText('That person is already a member.'),
      ).toBeTruthy(),
    );
    // Still on the form — the user can correct and retry.
    expect(screen.getByText('Create invite')).toBeTruthy();
  });
});
