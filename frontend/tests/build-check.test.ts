// @vitest-environment jsdom
/**
 * An old tab must not keep running old code after a deploy
 * (docs/chat-media/STORE-ALWAYS.md §3, 2026-10-03) — the pieces without the
 * app: the server's build id and its route, the page's own id, the comparison
 * (one in flight, no storms, failures silent), and the draft a reload keeps.
 */

import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  BUILD_CHECK_MIN_GAP_MS,
  BUILD_META_NAME,
  createBuildCheck,
  fetchServerBuildId,
  pageBuildId,
  RELOAD_DRAFT_KEY,
  saveReloadDraft,
  takeReloadDraft,
} from '@/lib/buildCheck';
import { readBuildId } from '@/lib/buildId';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.doUnmock('@/lib/buildId');
  vi.resetModules();
  document.head.innerHTML = '';
  window.sessionStorage.clear();
  window.history.replaceState(null, '', '/');
});

describe('the server build id', () => {
  it('is .next/BUILD_ID under the working directory, trimmed and shape-checked', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'build-id-'));
    expect(readBuildId(dir)).toBeNull();
    mkdirSync(path.join(dir, '.next'));
    writeFileSync(path.join(dir, '.next', 'BUILD_ID'), 'Ab3_x-9QzKp2\n');
    expect(readBuildId(dir)).toBe('Ab3_x-9QzKp2');
    writeFileSync(path.join(dir, '.next', 'BUILD_ID'), '<script>');
    expect(readBuildId(dir)).toBeNull();
  });

  it('GET /api/version answers it, uncached, with nothing else', async () => {
    vi.doMock('@/lib/buildId', () => ({ serverBuildId: () => 'build-new' }));
    const { GET } = await import('@/app/api/version/route');
    const res = GET();
    expect(res.status).toBe(200);
    expect(res.headers.get('cache-control')).toBe('no-store');
    expect(await res.json()).toEqual({ build: 'build-new' });
  });

  it('GET /api/version says null outside production (next dev, tests)', async () => {
    const { GET } = await import('@/app/api/version/route');
    expect(await GET().json()).toEqual({ build: null });
  });
});

describe('the page build id', () => {
  it('is read from the techsara-build meta, and is null without one', () => {
    expect(pageBuildId()).toBeNull();
    const meta = document.createElement('meta');
    meta.name = BUILD_META_NAME;
    meta.content = 'build-old';
    document.head.append(meta);
    expect(pageBuildId()).toBe('build-old');
  });
});

describe('fetchServerBuildId', () => {
  it('reads {build}, uncached; anything else is unknown, never a new build', async () => {
    const fetchMock = vi.fn(async () => Response.json({ build: 'build-new' }));
    vi.stubGlobal('fetch', fetchMock);
    expect(await fetchServerBuildId()).toBe('build-new');
    expect(fetchMock).toHaveBeenCalledWith('/api/version', { cache: 'no-store' });
    for (const answer of [
      () => Response.json({ build: null }),
      () => Response.json({}),
      () => new Response('nope', { status: 502 }),
      () => {
        throw new TypeError('Failed to fetch');
      },
    ]) {
      vi.stubGlobal('fetch', vi.fn(async () => answer()));
      expect(await fetchServerBuildId()).toBeNull();
    }
  });
});

describe('createBuildCheck', () => {
  function setup(answers: Array<string | null>) {
    let clock = 1_000_000;
    let release: (() => void) | null = null;
    let hold = false;
    const fetchBuild = vi.fn(async () => {
      if (hold) await new Promise<void>((r) => (release = r));
      return answers.length > 1 ? answers.shift()! : answers[0];
    });
    const onNewBuild = vi.fn();
    const check = createBuildCheck({
      pageBuild: 'build-old',
      onNewBuild,
      fetchBuild,
      now: () => clock,
    });
    return {
      check,
      fetchBuild,
      onNewBuild,
      tick: (ms: number) => {
        clock += ms;
      },
      holdNext: () => {
        hold = true;
      },
      releaseHeld: () => {
        hold = false;
        release?.();
      },
    };
  }

  it('equal ids do nothing', async () => {
    const t = setup(['build-old']);
    await t.check.check();
    expect(t.fetchBuild).toHaveBeenCalledTimes(1);
    expect(t.onNewBuild).not.toHaveBeenCalled();
    expect(t.check.newBuild).toBe(false);
  });

  it('a different id is reported once, and checking then stops', async () => {
    const t = setup(['build-new']);
    await t.check.check();
    expect(t.onNewBuild).toHaveBeenCalledWith('build-new');
    t.tick(BUILD_CHECK_MIN_GAP_MS * 10);
    await t.check.check();
    expect(t.fetchBuild).toHaveBeenCalledTimes(1);
    expect(t.onNewBuild).toHaveBeenCalledTimes(1);
    expect(t.check.newBuild).toBe(true);
  });

  it('one in flight at a time, and none closer than the gap', async () => {
    const t = setup(['build-old']);
    t.holdNext();
    const first = t.check.check();
    // A focus and a visibilitychange together, then a burst of alt-tabs.
    await t.check.check();
    await t.check.check();
    t.releaseHeld();
    await first;
    expect(t.fetchBuild).toHaveBeenCalledTimes(1);
    t.tick(BUILD_CHECK_MIN_GAP_MS - 1);
    await t.check.check();
    expect(t.fetchBuild).toHaveBeenCalledTimes(1);
    t.tick(1);
    await t.check.check();
    expect(t.fetchBuild).toHaveBeenCalledTimes(2);
  });

  it('a failure is silent and is tried again after the gap', async () => {
    const t = setup([null, 'build-new']);
    await t.check.check();
    expect(t.onNewBuild).not.toHaveBeenCalled();
    t.tick(BUILD_CHECK_MIN_GAP_MS);
    await t.check.check();
    expect(t.onNewBuild).toHaveBeenCalledWith('build-new');
  });
});

describe('the draft a reload keeps', () => {
  it('survives exactly one reload of the same page', () => {
    window.history.replaceState(null, '', '/?c=conv-1');
    saveReloadDraft('half a question about the invoice');
    expect(window.sessionStorage.getItem(RELOAD_DRAFT_KEY)).not.toBeNull();
    expect(takeReloadDraft()).toBe('half a question about the invoice');
    expect(takeReloadDraft()).toBeNull();
    expect(window.sessionStorage.getItem(RELOAD_DRAFT_KEY)).toBeNull();
  });

  it('is not put into another page, and blank text is not kept', () => {
    window.history.replaceState(null, '', '/?c=conv-1');
    saveReloadDraft('   ');
    expect(window.sessionStorage.getItem(RELOAD_DRAFT_KEY)).toBeNull();
    saveReloadDraft('for conv-1 only');
    window.history.replaceState(null, '', '/?c=conv-2');
    expect(takeReloadDraft()).toBeNull();
    // Read once even when it does not match: never offered twice.
    expect(window.sessionStorage.getItem(RELOAD_DRAFT_KEY)).toBeNull();
  });
});
