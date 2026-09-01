// @vitest-environment jsdom
/**
 * Sidebar account row + menu (enterprise auth retrofit): menu items follow
 * the server-granted capabilities (member vs admin), logout POSTs then
 * hard-redirects, Escape closes and hands focus back, "Settings" opens the
 * settings dialog, and the /api/auth/me probe is cached module-level.
 */

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  AccountMenu,
  clearAccountCache,
  performLogout,
} from '@/components/AccountMenu';
import type { FetchLike } from '@/lib/auth';

afterEach(cleanup);
beforeEach(() => {
  clearAccountCache();
  // Loader (used inside the settings dialog) renders a <video>.
  HTMLMediaElement.prototype.play =
    HTMLMediaElement.prototype.play ?? (async () => undefined);
});

const MEMBER = {
  username: 'naman',
  user: { id: 1, name: 'Naman Jain', email: 'naman@techsara.test' },
  workspace: { id: 'ws1', name: 'TechSara Solutions', role: 'member' },
  capabilities: [] as string[],
};

const ADMIN = {
  ...MEMBER,
  workspace: { ...MEMBER.workspace, role: 'admin' },
  capabilities: ['members.read', 'workspace_content.read'],
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** /api/auth/me + /api/auth/logout stub; everything else is a loud 500. */
function meFetch(me: unknown) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === '/api/auth/me') return json(me);
    if (url === '/api/auth/logout' && init?.method === 'POST') {
      return json({ ok: true });
    }
    return json({ detail: `unexpected ${url}` }, 500);
  });
}

type MeFetch = ReturnType<typeof meFetch>;

function asFetch(fn: MeFetch): FetchLike {
  return fn as unknown as FetchLike;
}

/** Render, wait for the identity to arrive, open the menu. */
async function openMenu(fn: MeFetch, navigate = vi.fn()) {
  render(<AccountMenu fetchFn={asFetch(fn)} navigate={navigate} />);
  const trigger = await screen.findByRole('button', { name: /Naman Jain/ });
  fireEvent.click(trigger);
  return { trigger, navigate };
}

describe('account row', () => {
  it('shows the avatar initial, name and email once /api/auth/me resolves', async () => {
    render(<AccountMenu fetchFn={asFetch(meFetch(MEMBER))} />);
    const trigger = await screen.findByRole('button', { name: /Naman Jain/ });
    expect(trigger.textContent).toContain('N'); // avatar initial
    expect(trigger.textContent).toContain('naman@techsara.test');
  });

  it('caches the /api/auth/me result module-level across mounts', async () => {
    const fn = meFetch(MEMBER);
    const first = render(<AccountMenu fetchFn={asFetch(fn)} />);
    await screen.findByRole('button', { name: /Naman Jain/ });
    first.unmount();
    render(<AccountMenu fetchFn={asFetch(fn)} />);
    await screen.findByRole('button', { name: /Naman Jain/ });
    const meCalls = fn.mock.calls.filter(
      (c) => String(c[0]) === '/api/auth/me',
    );
    expect(meCalls.length).toBe(1);
  });
});

describe('menu items follow capabilities', () => {
  it('member: no Workspace settings; the rest of the items are there', async () => {
    await openMenu(meFetch(MEMBER));
    const menu = screen.getByRole('menu', { name: 'Account' });
    expect(
      within(menu).queryByRole('menuitem', { name: 'Workspace settings' }),
    ).toBeNull();
    for (const label of ['Personalization', 'Settings', 'Help', 'Log out']) {
      expect(within(menu).getByRole('menuitem', { name: label })).toBeTruthy();
    }
    expect(within(menu).getByText('TechSara Solutions')).toBeTruthy();
    expect(within(menu).getByText('Enterprise')).toBeTruthy();
  });

  it('admin (members.read): Workspace settings links to /admin', async () => {
    await openMenu(meFetch(ADMIN));
    const menu = screen.getByRole('menu', { name: 'Account' });
    const item = within(menu).getByRole('menuitem', {
      name: 'Workspace settings',
    });
    expect(item.getAttribute('href')).toBe('/admin');
  });
});

describe('log out', () => {
  it('POSTs /api/auth/logout then hard-redirects to /login', async () => {
    const fn = meFetch(MEMBER);
    const { navigate } = await openMenu(fn);
    fireEvent.click(screen.getByRole('menuitem', { name: 'Log out' }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/login'));
    const calls = fn.mock.calls.map((c) => [String(c[0]), c[1]?.method]);
    expect(calls).toContainEqual(['/api/auth/logout', 'POST']);
  });

  it('performLogout still redirects when the POST fails (offline)', async () => {
    const navigate = vi.fn();
    const failing = (async () => {
      throw new Error('offline');
    }) as unknown as FetchLike;
    await performLogout(failing, navigate);
    expect(navigate).toHaveBeenCalledWith('/login');
  });
});

describe('keyboard and dialog wiring', () => {
  it('Escape closes the menu and returns focus to the trigger', async () => {
    const { trigger } = await openMenu(meFetch(MEMBER));
    expect(screen.getByRole('menu', { name: 'Account' })).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('menu', { name: 'Account' })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("'Settings' opens the settings dialog on the profile section", async () => {
    await openMenu(meFetch(MEMBER));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Settings' }));
    const dialog = screen.getByRole('dialog', { name: 'Settings' });
    expect(
      within(dialog).getAllByText('naman@techsara.test').length,
    ).toBeGreaterThan(0);
    expect(within(dialog).getByText('TechSara Solutions')).toBeTruthy();
    // The menu itself is gone once the dialog is up.
    expect(screen.queryByRole('menu', { name: 'Account' })).toBeNull();
  });
});
