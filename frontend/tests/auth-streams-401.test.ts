/**
 * Session death in the streaming layer (enterprise auth retrofit).
 *
 * A 401 on a send is not a message: the old path persisted a "Something
 * went wrong" bubble INTO the stored thread — junk turns for something no
 * retry can fix. Pinned here: an UNAUTHENTICATED failure drops the
 * placeholder, persists nothing, and routes to sign-in; every other
 * failure keeps today's behavior (the error bubble IS the record). The 8s
 * /api/chat/active poll is the app's heartbeat, so its 401 is where a
 * mid-session sign-out surfaces — it must redirect too, not report
 * "nothing active".
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const redirects: string[] = [];
vi.mock('../lib/auth', () => ({
  redirectToLogin: () => {
    redirects.push('/login');
  },
  // 2026-09-03: the streaming layer routes through handleSessionEnd, which
  // asks /auth/me why the session ended; with no explanation it lands on
  // sign-in exactly as redirectToLogin did.
  handleSessionEnd: async () => {
    redirects.push('/login');
  },
}));

const saved: Array<{ id: string; messages: unknown[] }> = [];
vi.mock('../lib/history', () => ({
  newId: () => `m-${Math.random().toString(36).slice(2, 10)}`,
  getHistoryStore: () => ({
    saveMessages: (id: string, messages: unknown[]) => {
      saved.push({ id, messages });
    },
    get: () => null,
    load: async () => null,
  }),
}));

import {
  fetchServerActive,
  getLiveStream,
  startStream,
} from '../lib/streams';
import { DEFAULT_PREFS } from '../lib/prefs';
import type { ChatMessage } from '../lib/types';

const turn = (content: string): ChatMessage => ({
  id: content,
  role: 'user',
  content,
  createdAt: 1,
});

function respond(status: number, body: unknown) {
  return async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });
}

beforeEach(() => {
  redirects.length = 0;
  saved.length = 0;
  vi.unstubAllGlobals();
});

describe('a 401 send never becomes a stored error bubble', () => {
  it('drops the stream, persists nothing, and redirects to /login', async () => {
    vi.stubGlobal('fetch', respond(401, { code: 'UNAUTHENTICATED' }));
    await startStream({
      conversationId: 'c-401',
      turns: [turn('hello')],
      prefs: DEFAULT_PREFS,
    });
    expect(saved).toHaveLength(0);
    expect(getLiveStream('c-401')).toBeNull();
    expect(redirects).toEqual(['/login']);
  });

  it('classifies a bare 401 (no code in the body) the same way', async () => {
    vi.stubGlobal('fetch', respond(401, { detail: 'Not signed in.' }));
    await startStream({
      conversationId: 'c-401-bare',
      turns: [turn('hello')],
      prefs: DEFAULT_PREFS,
    });
    expect(saved).toHaveLength(0);
    expect(redirects).toEqual(['/login']);
  });

  it('a 500 still persists the error bubble and does NOT redirect', async () => {
    vi.stubGlobal('fetch', respond(500, { code: 'APPLICATION_ERROR' }));
    await startStream({
      conversationId: 'c-500',
      turns: [turn('hello')],
      prefs: DEFAULT_PREFS,
    });
    expect(redirects).toHaveLength(0);
    expect(saved).toHaveLength(1);
    const last = saved[0].messages.at(-1) as {
      status?: string;
      errorCode?: string;
      errorStatus?: number;
    };
    expect(last.status).toBe('error');
    expect(last.errorCode).toBe('APPLICATION_ERROR');
    expect(last.errorStatus).toBe(500);
  });
});

describe('the 8s active poll treats 401 as session death', () => {
  it('redirects to /login and reports nothing active', async () => {
    vi.stubGlobal('fetch', respond(401, { detail: 'Not signed in.' }));
    await expect(fetchServerActive()).resolves.toEqual([]);
    expect(redirects).toEqual(['/login']);
  });

  it('an ordinary failure stays the quiet empty list', async () => {
    vi.stubGlobal('fetch', respond(503, { detail: 'down' }));
    await expect(fetchServerActive()).resolves.toEqual([]);
    expect(redirects).toHaveLength(0);
  });

  it('a healthy answer passes through', async () => {
    vi.stubGlobal('fetch', respond(200, { active: ['c-1', 'c-2'] }));
    await expect(fetchServerActive()).resolves.toEqual(['c-1', 'c-2']);
    expect(redirects).toHaveLength(0);
  });
});
