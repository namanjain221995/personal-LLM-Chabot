// @vitest-environment jsdom
/**
 * NEW-25 — scrolling while an answer is being generated.
 *
 * Two separate claims are tested here, because the bug had two mechanisms:
 *
 *   1. A native scroll gesture must cost the app NOTHING. There is no scroll
 *      listener any more, so a gesture performs no layout reads and causes no
 *      React work at all — the browser is left to scroll.
 *
 *   2. Auto-follow must not fight the user. Once the user scrolls away from
 *      the bottom, streaming updates must stop writing the scroll position,
 *      and following must resume when they come back.
 *
 * `IntersectionObserver` and `requestAnimationFrame` are both stubbed with
 * manual queues, so "the viewport moved" and "a frame elapsed" are function
 * calls and nothing depends on timing.
 */

import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import type { ChatMessage } from '@/lib/types';

const HISTORY = 40;

const counters = { layoutReads: 0, scrollWrites: 0, rowRenders: 0 };

const history = Array.from({ length: HISTORY }, (_, i) => ({
  id: `h${i}`,
  role: i % 2 === 0 ? 'user' : 'assistant',
  content: `Historical message ${i}.`,
  status: 'done',
  createdAt: 1000 + i,
})) as ChatMessage[];

vi.mock('@/components/MessageRow', async (orig) => {
  const actual = (await orig()) as typeof import('@/components/MessageRow');
  const { createElement, memo } = await import('react');
  return {
    ...actual,
    MessageRow: memo((p: Record<string, unknown>) => {
      counters.rowRenders += 1;
      return createElement(actual.MessageRow as never, p as never);
    }),
  };
});

const conversation = { id: 'conv-1', title: 'T', messages: history, createdAt: 0, updatedAt: 0 };
vi.mock('@/lib/history', () => ({
  newId: () => `m${Math.random().toString(36).slice(2, 10)}`,
  setEvictListener: () => undefined,
  rebuildHistoryStore: async () => { throw new Error('x'); },
  getHistoryStore: () => ({
    ready: async () => undefined,
    list: () => [{ id: 'conv-1', title: 'T', createdAt: 0, updatedAt: 0 }],
    listArchived: () => [], get: () => conversation,
    create: (t: string) => ({ id: 'conv-1', title: t, messages: [], createdAt: 0, updatedAt: 0 }),
    saveMessages: () => undefined, load: async () => conversation,
    setActiveUser: () => false, wipeLocal: async () => undefined,
    migrateLocalConversations: async () => 0, refresh: async () => true,
    refreshArchived: async () => true, generateTitle: async () => undefined,
    truncateMessages: async () => undefined, setMessageFeedback: async () => undefined,
    exportMarkdown: async () => null, remove: () => undefined, rename: () => undefined,
    setPinned: () => undefined, setArchived: () => undefined,
  }),
}));
vi.mock('@/lib/auth', () => ({
  fetchMe: async () => ({ ok: true, username: 't', user: null, features: {} }),
  userScopeKey: () => 't', redirectToLogin: () => undefined, handleSessionEnd: () => undefined,
}));
vi.mock('@/lib/salesforceApi', () => ({
  fetchSalesforceContext: async () => ({ options: [], pending: null }),
  cancelClarification: async () => undefined, shouldShowStarter: () => false,
}));
vi.mock('@/lib/compact', () => ({ isCompacting: () => false, requestCompact: async () => null }));

/** Every live IntersectionObserver, so a test can move the viewport by hand. */
interface FakeObserver {
  rootMargin: string;
  targets: Element[];
  fire: (isIntersecting: boolean) => void;
  disconnected: boolean;
}
let observers: FakeObserver[] = [];
/** The tight observer drives auto-follow; the 80px one drives the button. */
const followObserver = () => observers.find((o) => o.rootMargin.includes('8px'))!;
const buttonObserver = () => observers.find((o) => o.rootMargin.includes('80px'))!;

let frames: (FrameRequestCallback | null)[] = [];
const paint = () => { const due = frames; frames = []; for (const f of due) if (f) f(0); };

let enqueue: ((s: string) => void) | null = null;
let listeners: { type: string; passive: boolean }[] = [];

beforeEach(() => {
  Object.keys(counters).forEach((k) => { (counters as Record<string, number>)[k] = 0; });
  observers = [];
  frames = [];
  listeners = [];
  enqueue = null;

  vi.stubGlobal('IntersectionObserver', class {
    private entry: FakeObserver;
    constructor(cb: IntersectionObserverCallback, options: IntersectionObserverInit) {
      this.entry = {
        rootMargin: options.rootMargin ?? '',
        targets: [],
        disconnected: false,
        fire: (isIntersecting) =>
          cb([{ isIntersecting } as IntersectionObserverEntry], this as never),
      };
      observers.push(this.entry);
    }
    observe(el: Element) { this.entry.targets.push(el); }
    unobserve() { /* not used */ }
    disconnect() { this.entry.disconnected = true; }
  });

  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { frames.push(cb); return frames.length; });
  vi.stubGlobal('cancelAnimationFrame', (h: number) => { frames[h - 1] = null; });
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false, media: q, onchange: null, addEventListener: () => undefined,
    removeEventListener: () => undefined, addListener: () => undefined,
    removeListener: () => undefined, dispatchEvent: () => false,
  }));

  // Count every layout read and every scroll write the app performs.
  for (const prop of ['scrollHeight', 'clientHeight', 'offsetHeight'] as const) {
    Object.defineProperty(Element.prototype, prop, {
      configurable: true, get: () => { counters.layoutReads += 1; return 5000; },
    });
  }
  Object.defineProperty(Element.prototype, 'scrollTop', {
    configurable: true,
    get: () => { counters.layoutReads += 1; return 0; },
    set: () => { counters.scrollWrites += 1; },
  });
  Element.prototype.getBoundingClientRect = function () {
    counters.layoutReads += 1;
    return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) };
  } as never;

  // Record how listeners are registered, so passivity can be asserted.
  const realAdd = Element.prototype.addEventListener;
  Element.prototype.addEventListener = function (this: Element, type: string, fn: never, opts?: never) {
    if (type === 'wheel' || type === 'touchmove' || type === 'touchstart') {
      listeners.push({ type, passive: Boolean((opts as { passive?: boolean } | undefined)?.passive) });
    }
    return realAdd.call(this, type, fn, opts);
  } as never;

  window.history.replaceState({}, '', '/?c=conv-1');
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const u = String(url);
    if (u === '/api/chat/active') return { ok: true, status: 200, json: async () => ({ active: [] }) };
    if (u.startsWith('/api/chat')) {
      const encoder = new TextEncoder();
      return { ok: true, status: 200, body: new ReadableStream<Uint8Array>({
        start(c) { enqueue = (x) => c.enqueue(encoder.encode(x)); } }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  }));
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.resetModules(); });

const token = (text: string) => `event: token\ndata: ${JSON.stringify({ text })}\n\n`;

async function mountStreaming() {
  const { ChatApp } = await import('@/components/ChatApp');
  const { Providers } = await import('@/components/Providers');
  const { startStream } = await import('@/lib/streams');
  const view = render(<Providers><ChatApp /></Providers>);
  await waitFor(() => expect(document.querySelectorAll('[data-chat-message-role]').length).toBe(HISTORY));
  await act(async () => {
    void startStream({
      conversationId: 'conv-1',
      turns: [...history, { id: 'u', role: 'user', content: 'Ask', status: 'done', createdAt: 9 } as ChatMessage],
      prefs: { model: 'fast', effort: 'low', agent: false, webSearch: false,
        deepResearch: false, salesforce: false, sfLive: false } as never,
    });
  });
  await waitFor(() => expect(enqueue).not.toBeNull());
  return view;
}

/**
 * One delta, then the two frames it takes to settle: the first commits the
 * stream's coalesced update, and the follow effect that runs after that commit
 * books its scroll write for the frame after.
 */
async function delta(text: string) {
  await act(async () => { enqueue!(token(text)); await Promise.resolve(); });
  await act(async () => { paint(); await Promise.resolve(); });
  await act(async () => { paint(); await Promise.resolve(); });
}

/**
 * The conversation scroller. Selected by the `relative` marker because the
 * sidebar's own list is also `overflow-y-auto` and comes first in the DOM.
 */
const scroller = () =>
  document.querySelector('div.relative.overflow-y-auto') as HTMLElement;

test('TEST 7: a native scroll gesture costs no layout reads and no React work', async () => {
  await mountStreaming();
  await delta('Answer. ');
  counters.layoutReads = 0;
  counters.rowRenders = 0;

  // 200 scroll events — what a single trackpad swipe produces.
  await act(async () => {
    for (let i = 0; i < 200; i += 1) scroller().dispatchEvent(new Event('scroll'));
    await Promise.resolve();
  });

  // There is no scroll listener at all: the browser scrolls, the app does not
  // measure the document 200 times and does not re-render.
  expect(counters.layoutReads).toBe(0);
  expect(counters.rowRenders).toBe(0);
});

test('TEST 7b: wheel and touch listeners are passive', async () => {
  await mountStreaming();
  const kinds = Object.fromEntries(listeners.map((l) => [l.type, l.passive]));
  expect(kinds.wheel).toBe(true);
  expect(kinds.touchstart).toBe(true);
  expect(kinds.touchmove).toBe(true);
});

test('TEST 8: scrolling up stops auto-follow; streaming stops writing scrollTop', async () => {
  await mountStreaming();
  await delta('First part. ');
  expect(counters.scrollWrites).toBeGreaterThan(0); // following at the bottom

  // The user rolls the wheel upward.
  await act(async () => {
    scroller().dispatchEvent(
      Object.assign(new Event('wheel'), { deltaY: -120 }) as WheelEvent,
    );
    await Promise.resolve();
  });
  counters.scrollWrites = 0;

  // Generation continues. The user's position must be left alone.
  for (let i = 0; i < 20; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await delta(`more ${i} `);
  }
  expect(counters.scrollWrites).toBe(0);

  // ...and the answer really did keep arriving underneath them.
  const rows = document.querySelectorAll('[data-chat-message-role="assistant"]');
  expect(rows[rows.length - 1].textContent).toContain('more 19');
});

test('TEST 8b: leaving the bottom by any means stops auto-follow', async () => {
  await mountStreaming();
  await delta('Start. ');

  // No wheel event — a scrollbar drag or Page Up. The sentinel leaves view.
  await act(async () => { followObserver().fire(false); await Promise.resolve(); });
  counters.scrollWrites = 0;

  for (let i = 0; i < 10; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await delta(`x${i} `);
  }
  expect(counters.scrollWrites).toBe(0);
});

test('TEST 9: returning to the bottom resumes following', async () => {
  await mountStreaming();
  await delta('Start. ');
  await act(async () => {
    scroller().dispatchEvent(Object.assign(new Event('wheel'), { deltaY: -120 }) as WheelEvent);
    await Promise.resolve();
  });
  await act(async () => { followObserver().fire(false); await Promise.resolve(); });
  counters.scrollWrites = 0;
  await delta('while away ');
  expect(counters.scrollWrites).toBe(0);

  // The user scrolls back down; the sentinel comes into view again.
  await act(async () => { followObserver().fire(true); await Promise.resolve(); });
  await delta('back at bottom ');
  expect(counters.scrollWrites).toBeGreaterThan(0);
});

test('TEST 9b: the Jump to latest button follows the 80px observer', async () => {
  await mountStreaming();
  await delta('Start. ');
  expect(document.body.textContent).not.toContain('Jump to latest');

  await act(async () => { buttonObserver().fire(false); await Promise.resolve(); });
  expect(document.body.textContent).toContain('Jump to latest');

  await act(async () => { buttonObserver().fire(true); await Promise.resolve(); });
  expect(document.body.textContent).not.toContain('Jump to latest');
});

test('TEST 10: many deltas in one frame produce ONE scroll write', async () => {
  await mountStreaming();
  await delta('Start. ');
  counters.scrollWrites = 0;

  await act(async () => {
    for (let i = 0; i < 25; i += 1) enqueue!(token(`t${i} `));
    await Promise.resolve();
  });
  // The commit, then the frame the follow books from its effect.
  await act(async () => { paint(); await Promise.resolve(); });
  await act(async () => { paint(); await Promise.resolve(); });

  expect(counters.scrollWrites).toBe(1);
});

test('TEST 11: unmount disconnects observers, removes listeners and cancels frames', async () => {
  const view = await mountStreaming();
  await delta('Start. ');
  expect(observers.length).toBe(2);

  const removed: string[] = [];
  const realRemove = Element.prototype.removeEventListener;
  Element.prototype.removeEventListener = function (this: Element, type: string, fn: never, opts?: never) {
    if (type === 'wheel' || type === 'touchmove' || type === 'touchstart') removed.push(type);
    return realRemove.call(this, type, fn, opts);
  } as never;

  // Leave a follow frame booked, then tear the view down.
  await act(async () => { enqueue!(token('pending')); await Promise.resolve(); });
  const errors: unknown[] = [];
  const spy = vi.spyOn(console, 'error').mockImplementation((...a) => errors.push(a));
  await act(async () => { view.unmount(); });

  expect(observers.every((o) => o.disconnected)).toBe(true);
  expect(removed.sort()).toEqual(['touchmove', 'touchstart', 'wheel']);

  counters.scrollWrites = 0;
  await act(async () => { paint(); await Promise.resolve(); });
  expect(counters.scrollWrites).toBe(0);
  expect(errors).toEqual([]);
  spy.mockRestore();
  Element.prototype.removeEventListener = realRemove;
});
