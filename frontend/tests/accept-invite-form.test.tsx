// @vitest-environment jsdom
/**
 * The /accept-invite form (workstream B).
 *
 * The token rides in ?token=... and is probed with GET
 * /api/auth/invitations/{token}. The orchestrator answers 404 for expired,
 * used, revoked and unknown alike, so the UI has exactly one "no longer
 * valid" state. A valid invitation pre-fills workspace + identity, enforces
 * the 10-character password floor client-side, POSTs the accept payload and
 * leaves via a full navigation (the accept auto-logs-in upstream).
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AcceptInviteForm } from '@/components/auth/AcceptInviteForm';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState({}, '', '/accept-invite');
});

const jsonResponse = (body: unknown, ok = true, status = 200) => ({
  ok,
  status,
  json: async () => body,
});

const INVITE = {
  email: 'asha@techsara.com',
  name: 'Asha Rao',
  role: 'member',
  workspace_name: 'TechSara Solutions',
  expires_at: '2026-09-08T00:00:00Z',
};

function setToken(token: string) {
  window.history.replaceState({}, '', `/accept-invite?token=${token}`);
}

describe('invalid invitations', () => {
  it('shows the no-longer-valid state on 404', async () => {
    setToken('tok-dead');
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: 'Not found' }, false, 404)),
    );
    render(<AcceptInviteForm navigate={vi.fn()} />);

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('This invitation is no longer valid');
    expect(alert.textContent).toContain('contact your workspace administrator');
    // No form to fill.
    expect(screen.queryByLabelText('Password')).toBeNull();
  });

  it('treats a missing token as invalid without calling the server', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    render(<AcceptInviteForm navigate={vi.fn()} />);

    await screen.findByRole('alert');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('a valid invitation', () => {
  it('loads the invite, pre-fills identity, posts the accept and redirects', async () => {
    setToken('tok-123');
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? jsonResponse({ username: 'asha', user: { id: 7 } })
        : jsonResponse(INVITE),
    );
    vi.stubGlobal('fetch', fetchMock);
    const navigate = vi.fn();
    render(<AcceptInviteForm navigate={navigate} />);

    // Probe hit the token URL.
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/auth/invitations/tok-123',
        expect.objectContaining({ cache: 'no-store' }),
      ),
    );

    // Workspace name + read-only invited email + pre-filled name.
    expect(
      await screen.findByRole('heading', { name: 'Join TechSara Solutions' }),
    ).toBeTruthy();
    const email = screen.getByLabelText('Email') as HTMLInputElement;
    expect(email.value).toBe('asha@techsara.com');
    expect(email.readOnly).toBe(true);
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe(
      'Asha Rao',
    );

    // The 10-character floor is hinted, and the privacy line is present.
    expect(screen.getByText('At least 10 characters.')).toBeTruthy();
    expect(
      screen.getByText(
        'Workspace content may be accessible to authorized administrators in accordance with company policy.',
      ),
    ).toBeTruthy();

    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'long-enough-pass' },
    });
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'long-enough-pass' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/'));
    const accept = fetchMock.mock.calls.find(
      ([, init]) => init?.method === 'POST',
    );
    expect(accept?.[0]).toBe('/api/auth/invitations/accept');
    expect(JSON.parse(accept?.[1]?.body as string)).toEqual({
      token: 'tok-123',
      name: 'Asha Rao',
      password: 'long-enough-pass',
    });
  });

  it('blocks a short password and a mismatched confirm client-side', async () => {
    setToken('tok-123');
    const fetchMock = vi.fn(async () => jsonResponse(INVITE));
    vi.stubGlobal('fetch', fetchMock);
    render(<AcceptInviteForm navigate={vi.fn()} />);

    await screen.findByRole('heading', { name: 'Join TechSara Solutions' });

    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'short' },
    });
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'short' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Password must be at least 10 characters.',
    );

    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'long-enough-pass' },
    });
    fireEvent.change(screen.getByLabelText('Confirm password'), {
      target: { value: 'long-enough-typo' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create account' }));
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Passwords do not match.',
    );

    // Only the initial GET probe ever went out.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
