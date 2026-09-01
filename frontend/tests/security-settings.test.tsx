// @vitest-environment jsdom
/**
 * Security + Sessions settings (enterprise auth retrofit): password change
 * happy path and 403/422/mismatch errors, the sessions list with its coarse
 * device labels and "This device" badge, and both revoke flows (per-row
 * {session_id}, "Log out other sessions" {others:true}) behind ConfirmDialog.
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
import { Providers } from '@/components/Providers';
import {
  describeUserAgent,
  PasswordSection,
  SessionsSection,
} from '@/components/SecuritySettings';
import type { FetchLike } from '@/lib/auth';

afterEach(cleanup);
beforeEach(() => {
  // Loader renders a <video>; jsdom's media element needs a play stub.
  HTMLMediaElement.prototype.play =
    HTMLMediaElement.prototype.play ?? (async () => undefined);
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const CHROME_LINUX_UA =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';
const IPHONE_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1';

/* ------------------------------------------------------------- user agent */

describe('describeUserAgent', () => {
  it('is coarse, never the raw string', () => {
    expect(describeUserAgent(CHROME_LINUX_UA)).toBe('Chrome · Linux');
    expect(describeUserAgent(IPHONE_UA)).toBe('Safari · iOS');
    expect(
      describeUserAgent(
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0',
      ),
    ).toBe('Edge · Windows');
    expect(
      describeUserAgent(
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0',
      ),
    ).toBe('Firefox · macOS');
    expect(describeUserAgent('curl/8.5.0')).toBe('API client');
    expect(describeUserAgent(null)).toBe('Unknown device');
    expect(describeUserAgent('')).toBe('Unknown device');
  });
});

/* --------------------------------------------------------------- password */

function renderPassword(fn: ReturnType<typeof vi.fn>) {
  render(
    <Providers>
      <PasswordSection fetchFn={fn as unknown as FetchLike} />
    </Providers>,
  );
}

function fillAndSubmit(current: string, next: string, confirm: string) {
  fireEvent.change(screen.getByLabelText('Current password'), {
    target: { value: current },
  });
  fireEvent.change(screen.getByLabelText('New password'), {
    target: { value: next },
  });
  fireEvent.change(screen.getByLabelText('Confirm new password'), {
    target: { value: confirm },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Change password' }));
}

describe('change password', () => {
  it('happy path: POSTs the contract body and toasts success', async () => {
    const fn = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      json({ ok: true }),
    );
    renderPassword(fn);
    fillAndSubmit('old-password-1', 'new-password-12', 'new-password-12');
    await screen.findByText('Password updated');
    expect(String(fn.mock.calls[0][0])).toBe('/api/auth/password');
    expect(fn.mock.calls[0][1]?.method).toBe('POST');
    expect(JSON.parse(String(fn.mock.calls[0][1]?.body))).toEqual({
      current_password: 'old-password-1',
      new_password: 'new-password-12',
    });
    // Fields are cleared after success.
    expect(
      (screen.getByLabelText('Current password') as HTMLInputElement).value,
    ).toBe('');
  });

  it('403 shows the wrong-current-password error inline, no toast', async () => {
    const fn = vi.fn(async (_i: RequestInfo | URL, _init?: RequestInit) =>
      json({ detail: 'Incorrect password.' }, 403),
    );
    renderPassword(fn);
    fillAndSubmit('wrong-password', 'new-password-12', 'new-password-12');
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Current password is incorrect.');
    expect(screen.queryByText('Password updated')).toBeNull();
  });

  it('422 surfaces the server detail inline', async () => {
    const fn = vi.fn(async (_i: RequestInfo | URL, _init?: RequestInit) =>
      json({ detail: 'New password is too common.' }, 422),
    );
    renderPassword(fn);
    fillAndSubmit('old-password-1', 'aaaaaaaaaaaa', 'aaaaaaaaaaaa');
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('New password is too common.');
  });

  it('mismatched confirmation never reaches the server', async () => {
    const fn = vi.fn(async (_i: RequestInfo | URL, _init?: RequestInit) =>
      json({ ok: true }),
    );
    renderPassword(fn);
    fillAndSubmit('old-password-1', 'new-password-12', 'different-pw-12');
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('New passwords do not match.');
    expect(fn).not.toHaveBeenCalled();
  });

  it('a too-short new password is rejected client-side (server rule mirrored)', async () => {
    const fn = vi.fn(async (_i: RequestInfo | URL, _init?: RequestInit) =>
      json({ ok: true }),
    );
    renderPassword(fn);
    fillAndSubmit('old-password-1', 'short', 'short');
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('at least 10 characters');
    expect(fn).not.toHaveBeenCalled();
  });
});

/* --------------------------------------------------------------- sessions */

const OTHER_SESSION = {
  id: 's-other',
  current: false,
  created_at: '2026-08-30T10:00:00Z',
  last_seen_at: '2026-08-31T09:00:00Z',
  user_agent: IPHONE_UA,
  ip: '10.0.0.9',
};
const THIS_SESSION = {
  id: 's-this',
  current: true,
  created_at: '2026-08-01T10:00:00Z',
  last_seen_at: '2026-09-01T08:00:00Z',
  user_agent: CHROME_LINUX_UA,
  ip: '10.0.0.2',
};

/** GET list / POST revoke router; after a revoke only THIS_SESSION remains. */
function sessionsFetch() {
  let revoked = false;
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === '/api/auth/sessions') {
      return json({
        sessions: revoked ? [THIS_SESSION] : [OTHER_SESSION, THIS_SESSION],
      });
    }
    if (url === '/api/auth/sessions/revoke' && init?.method === 'POST') {
      revoked = true;
      return json({ revoked: 1 });
    }
    return json({ detail: `unexpected ${url}` }, 500);
  });
}

function renderSessions(fn: ReturnType<typeof sessionsFetch>) {
  render(
    <Providers>
      <SessionsSection fetchFn={fn as unknown as FetchLike} />
    </Providers>,
  );
}

describe('sessions list', () => {
  it('shows coarse device labels, current-first, with a This device badge', async () => {
    renderSessions(sessionsFetch());
    await screen.findByText('Chrome · Linux');
    expect(screen.getByText('Safari · iOS')).toBeTruthy();
    const rows = screen.getAllByRole('listitem');
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('This device');
    expect(rows[0].textContent).toContain('Chrome · Linux');
    // Only the OTHER row gets a per-row Sign out; this device logs out via
    // the account menu instead.
    expect(screen.getAllByRole('button', { name: 'Sign out' }).length).toBe(1);
  });

  it('per-row Sign out confirms, POSTs {session_id}, and refreshes', async () => {
    const fn = sessionsFetch();
    renderSessions(fn);
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    const dialog = screen.getByRole('alertdialog', {
      name: 'Sign out this session?',
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Sign out' }));
    await waitFor(() => {
      const call = fn.mock.calls.find(
        (c) => String(c[0]) === '/api/auth/sessions/revoke',
      );
      expect(call).toBeTruthy();
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({
        session_id: 's-other',
      });
    });
    // The revoked row disappears after the reload.
    await waitFor(() =>
      expect(screen.queryByText('Safari · iOS')).toBeNull(),
    );
  });

  it('Log out other sessions confirms then POSTs {others:true}', async () => {
    const fn = sessionsFetch();
    renderSessions(fn);
    fireEvent.click(
      await screen.findByRole('button', { name: 'Log out other sessions' }),
    );
    const dialog = screen.getByRole('alertdialog', {
      name: 'Log out other sessions?',
    });
    fireEvent.click(
      within(dialog).getByRole('button', { name: 'Log out others' }),
    );
    await waitFor(() => {
      const call = fn.mock.calls.find(
        (c) => String(c[0]) === '/api/auth/sessions/revoke',
      );
      expect(call).toBeTruthy();
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({ others: true });
    });
  });

  it('canceling the confirm dialog revokes nothing', async () => {
    const fn = sessionsFetch();
    renderSessions(fn);
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    const dialog = screen.getByRole('alertdialog', {
      name: 'Sign out this session?',
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(
      fn.mock.calls.some((c) => String(c[0]) === '/api/auth/sessions/revoke'),
    ).toBe(false);
    expect(screen.getByText('Safari · iOS')).toBeTruthy();
  });
});
