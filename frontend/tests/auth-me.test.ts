/**
 * fetchMe (enterprise auth retrofit): the full ME_PAYLOAD comes through,
 * failures keep their status (0 = network, so the app can stay usable
 * offline while a 401 routes to sign-in), and userScopeKey derives the
 * STABLE cache key — the numeric user id, never the renameable name.
 */
import { describe, expect, it } from 'vitest';

import { fetchMe, userScopeKey, type FetchLike } from '../lib/auth';

const ME_PAYLOAD = {
  username: 'naman',
  user: { id: 7, name: 'Naman', email: 'naman@techsara.test' },
  workspace: { id: 'ws-1', name: 'TechSara', role: 'admin' },
  capabilities: ['members.read', 'workspace_content.read'],
};

const respond = (status: number, body: unknown): FetchLike =>
  (async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    })) as FetchLike;

describe('fetchMe — the full payload', () => {
  it('returns user, workspace and capabilities', async () => {
    const me = await fetchMe(respond(200, ME_PAYLOAD));
    expect(me).toEqual({ ok: true, ...ME_PAYLOAD });
  });

  it('keeps reading the legacy bare {username} shape', async () => {
    const me = await fetchMe(respond(200, { username: 'local', local: true }));
    expect(me).toEqual({
      ok: true,
      username: 'local',
      user: null,
      workspace: null,
      capabilities: [],
    });
  });

  it('drops non-string entries from capabilities', async () => {
    const me = await fetchMe(
      respond(200, { ...ME_PAYLOAD, capabilities: ['audit.read', 7, null] }),
    );
    expect(me.ok && me.capabilities).toEqual(['audit.read']);
  });

  it('normalizes an unknown role to member', async () => {
    const me = await fetchMe(
      respond(200, {
        ...ME_PAYLOAD,
        workspace: { id: 'ws-1', name: 'TechSara', role: 'owner' },
      }),
    );
    expect(me.ok && me.workspace?.role).toBe('member');
  });

  it('treats a 200 that names nobody as a failure', async () => {
    const me = await fetchMe(respond(200, {}));
    expect(me).toEqual({ ok: false, status: 200 });
  });
});

describe('fetchMe — failures keep their status', () => {
  it.each([401, 403, 502])('reports %i as-is', async (status) => {
    const me = await fetchMe(respond(status, { detail: 'no' }));
    expect(me).toEqual({ ok: false, status });
  });

  it('reports a network failure as status 0 (stay usable offline)', async () => {
    const me = await fetchMe((async () => {
      throw new TypeError('fetch failed');
    }) as FetchLike);
    expect(me).toEqual({ ok: false, status: 0 });
  });
});

describe('userScopeKey — the stable cache key', () => {
  it('is u<id> when the server sent a user object', () => {
    expect(
      userScopeKey({ user: ME_PAYLOAD.user, username: 'naman' }),
    ).toBe('u7');
  });

  it('survives a display-name change (the id does not move)', () => {
    const before = userScopeKey({ user: ME_PAYLOAD.user, username: 'naman' });
    const after = userScopeKey({
      user: ME_PAYLOAD.user,
      username: 'naman.jain',
    });
    expect(after).toBe(before);
  });

  it('falls back to the username for a legacy backend', () => {
    expect(userScopeKey({ user: null, username: 'local' })).toBe('local');
  });
});
