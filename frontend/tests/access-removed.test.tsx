// @vitest-environment jsdom
/**
 * The removed/deactivated-member page and the routing rule that reaches it
 * (2026-09-03). Before: a removed member got a bare 401, the sign-in form,
 * and "Incorrect email or password" — nothing ever said their access was
 * removed. Now /auth/me explains (only to the browser that held the real
 * cookie), handleSessionEnd wipes local data and lands on /access-removed,
 * and the page says what happened, when, and who to contact.
 */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// Hoisted: the page reads its facts from the URL through next/navigation,
// which has no app router under jsdom. The holder is filled per test.
const search = { params: new URLSearchParams() };
vi.mock('next/navigation', () => ({
  useSearchParams: () => search.params,
}));

import {
  handleSessionEnd,
  sessionEndRoute,
  isAccessEnded,
  type FetchLike,
  type MeFailure,
} from '../lib/auth';
import {
  AccessRemoved,
  accessRemovedCopy,
  parseContact,
} from '../components/auth/AccessRemoved';

afterEach(cleanup);

const removed: MeFailure = {
  ok: false,
  status: 401,
  code: 'account_removed',
  workspace: 'TechSara',
  endedAt: '2026-09-03T02:40:00+00:00',
  contact: [
    { email: 'root@techsara.test', name: 'Naman Jain' },
    { email: 'ops@techsara.test', name: '' },
  ],
};

describe('sessionEndRoute', () => {
  it('sends a removed account to the explanation page with what it needs', () => {
    const url = sessionEndRoute(removed);
    expect(url.startsWith('/access-removed?')).toBe(true);
    const params = new URL(`http://x${url}`).searchParams;
    expect(params.get('code')).toBe('account_removed');
    expect(params.get('ws')).toBe('TechSara');
    expect(params.get('at')).toBe('2026-09-03T02:40:00+00:00');
    expect(params.getAll('contact')).toEqual([
      'Naman Jain <root@techsara.test>',
      'ops@techsara.test',
    ]);
  });

  it('sends every other end to sign-in', () => {
    for (const code of ['session_expired', 'session_revoked', 'signed_out', undefined] as const) {
      expect(sessionEndRoute({ ok: false, status: 401, code })).toBe('/login');
      expect(isAccessEnded(code)).toBe(false);
    }
    expect(isAccessEnded('account_disabled')).toBe(true);
  });
});

describe('handleSessionEnd', () => {
  it('probes /auth/me when given nothing and routes on the answer', async () => {
    const fetchFn: FetchLike = (async () =>
      new Response(
        JSON.stringify({ detail: { code: 'account_disabled', workspace: 'TechSara', contact: [] } }),
        { status: 401, headers: { 'content-type': 'application/json' } },
      )) as FetchLike;
    const nav = { assign: vi.fn() };
    await handleSessionEnd(undefined, fetchFn, nav);
    expect(nav.assign).toHaveBeenCalledTimes(1);
    expect(nav.assign.mock.calls[0][0]).toMatch(/^\/access-removed\?code=account_disabled/);
  });

  it('a plain sign-out still goes to /login', async () => {
    const nav = { assign: vi.fn() };
    await handleSessionEnd({ ok: false, status: 401 }, fetch, nav);
    expect(nav.assign).toHaveBeenCalledWith('/login');
  });
});

describe('the page copy', () => {
  it('distinguishes removed from deactivated and names the workspace', () => {
    const r = accessRemovedCopy('account_removed', 'TechSara');
    expect(r.title).toBe('Your access has been removed');
    expect(r.lead).toContain('the TechSara workspace');
    const d = accessRemovedCopy('account_disabled', '');
    expect(d.title).toBe('Your account has been deactivated');
    expect(d.lead).toContain('this workspace');
    expect(d.ask).toMatch(/reactivate/);
    expect(d.button).toBe('Back to sign-in');
    expect(r.button).toBe('Sign in with a different account');
  });

  it('parses "Name <email>" and bare emails, and rejects junk', () => {
    expect(parseContact('Naman Jain <root@techsara.test>')).toEqual({
      name: 'Naman Jain',
      email: 'root@techsara.test',
    });
    expect(parseContact('ops@techsara.test')).toEqual({ name: '', email: 'ops@techsara.test' });
    expect(parseContact('not an email')).toBeNull();
  });
});

describe('the page', () => {
  it('renders the explanation, the contacts as mailto links, and a way to sign in as someone else', async () => {
    search.params = new URLSearchParams(sessionEndRoute(removed).split('?')[1]);
    render(<AccessRemoved />);
    expect(await screen.findByText('Your access has been removed')).toBeTruthy();
    expect(screen.getByText(/removed your access to the TechSara workspace/)).toBeTruthy();
    const links = screen.getAllByRole('link');
    const mailtos = links.filter((a) => (a as HTMLAnchorElement).href.startsWith('mailto:'));
    expect(mailtos.map((a) => a.textContent)).toEqual(['Naman Jain', 'ops@techsara.test']);
    expect((mailtos[0] as HTMLAnchorElement).href).toContain('mailto:root@techsara.test');
    const signIn = links.find((a) => a.textContent?.includes('different account')) as HTMLAnchorElement;
    expect(signIn.getAttribute('href')).toBe('/login');
  });
});
