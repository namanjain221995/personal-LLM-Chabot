/**
 * The docs' Console-link decision (components/docs/consoleAccess.ts) and the
 * private endpoint that serves it (app/api/docs/console-access/route.ts).
 *
 * It asks the console's own gate, and every answer that is not "allowed" —
 * a refusal, a signed-out reader, an unreachable server, a rejection, a
 * stall — means no link. None of them may reject: the answer is sent as
 * plain JSON, and a thrown error would turn "no" into a 500.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/headers', () => ({ cookies: async () => ({ get: () => undefined }) }));

import { GET } from '@/app/api/docs/console-access/route';
import { consoleLinkAllowed } from '@/components/docs/consoleAccess';

afterEach(() => {
  vi.useRealTimers();
});

describe('whether the docs draw the Console link', () => {
  it('draws it only for a session the console allows', async () => {
    await expect(consoleLinkAllowed(async () => ({ state: 'allowed' }))).resolves.toBe(true);
    await expect(consoleLinkAllowed(async () => ({ state: 'refused' }))).resolves.toBe(false);
    await expect(consoleLinkAllowed(async () => ({ state: 'signed-out' }))).resolves.toBe(false);
    await expect(consoleLinkAllowed(async () => ({ state: 'unavailable' }))).resolves.toBe(false);
  });

  it('resolves false instead of rejecting when the check throws', async () => {
    await expect(
      consoleLinkAllowed(async () => {
        throw new Error('boom');
      }),
    ).resolves.toBe(false);
  });

  it('gives up after its deadline, so a stalled orchestrator costs the link and not the page', async () => {
    vi.useFakeTimers();
    const pending = consoleLinkAllowed(() => new Promise(() => undefined), 3000);
    await vi.advanceTimersByTimeAsync(3000);
    await expect(pending).resolves.toBe(false);
  });

  it('asks nothing upstream for a reader with no session cookie', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    await expect(consoleLinkAllowed()).resolves.toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe('GET /api/docs/console-access', () => {
  it('answers a signed-out reader 200 with allowed false, never 401, and never lets a cache keep it', async () => {
    // The docs are public: a 401 would log a red console error on every
    // signed-out page view, and a shared cache must never hand one reader's
    // answer to another.
    const res = await GET();
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ allowed: false });
    expect(res.headers.get('cache-control')).toBe('private, no-store');
  });
});
