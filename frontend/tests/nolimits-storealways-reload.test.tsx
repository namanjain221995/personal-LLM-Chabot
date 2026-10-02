// @vitest-environment jsdom
/**
 * The no-limits release meets store-always's reload (integration, 2026-10-03).
 *
 * A send whose photos are over the 48 MiB inline budget uploads them to
 * POST /api/chat-media BEFORE its /chat request goes out
 * (docs/chat-media/LIMITS.md), and a tab left on an old build reloads by
 * itself as soon as a reload would lose nothing (STORE-ALWAYS.md §3). From
 * the click on Send to the /chat request the composer is already empty and
 * no dataset or document upload is pending, so the only thing that keeps the
 * reload from cutting the send in half is the stream startStream registers
 * BEFORE it uploads the photos. Pinned here through the real ChatApp,
 * Composer and startStream; only the network, the history store, the
 * browser's shrink and the reload itself are stubbed.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DownscaledImage } from '@/lib/images';
import type { ChatRequestBody } from '@/lib/orchestrator';
import type { ChatMessage } from '@/lib/types';

/* ------------------------------------------------------------------ state */

const CONV = 'conv-1';
const MB = 1024 * 1024;
let stored: ChatMessage[] = [];
let chatBodies: ChatRequestBody[] = [];
let mediaPosts: FormData[] = [];
let versionReads = 0;
/** Holds every POST /api/chat-media until the test lets the photos land. */
let releaseMedia: () => void = () => undefined;
let mediaGate: Promise<void> = Promise.resolve();
let releaseStream: (() => void) | null = null;
/** jsdom has no canvas: every photo is sent as it is. */
const downscale = vi.fn<(file: File) => Promise<DownscaledImage | null>>(async () => null);

vi.mock('@/lib/images', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/images')>()),
  downscaleImageFile: (file: File) => downscale(file),
}));
vi.mock('@/lib/history', () => ({
  newId: () => `m${Math.random().toString(36).slice(2, 10)}`,
  setEvictListener: () => undefined,
  rebuildHistoryStore: async () => {
    throw new Error('unexpected account switch in test');
  },
  getHistoryStore: () => ({
    ready: async () => undefined,
    list: () => [],
    listArchived: () => [],
    get: (id: string) =>
      id === CONV && stored.length
        ? { id, title: 'photos', createdAt: 0, updatedAt: 0, messages: stored }
        : null,
    create: (title: string) => ({ id: CONV, title, messages: [], createdAt: 0, updatedAt: 0 }),
    saveMessages: (_id: string, msgs: ChatMessage[]) => {
      stored = msgs;
    },
    amendMessages: async (_id: string, msgs: ChatMessage[]) => {
      stored = msgs;
    },
    flush: async () => undefined,
    load: async () => null,
    setActiveUser: () => false,
    wipeLocal: async () => undefined,
    migrateLocalConversations: async () => 0,
    refresh: async () => true,
    refreshArchived: async () => true,
    generateTitle: async () => undefined,
    truncateMessages: async () => undefined,
    setMessageFeedback: async () => undefined,
    exportMarkdown: async () => null,
    remove: () => undefined,
    rename: () => undefined,
    setPinned: () => undefined,
    setArchived: () => undefined,
  }),
}));
vi.mock('@/lib/auth', () => ({
  fetchMe: async () => ({ ok: true, username: 'tester', user: null }),
  userScopeKey: () => 'tester',
  redirectToLogin: () => undefined,
  handleSessionEnd: async () => undefined,
}));
vi.mock('@/lib/salesforceApi', () => ({
  fetchSalesforceContext: async () => ({ options: [], pending: null }),
  cancelClarification: async () => undefined,
  shouldShowStarter: () => false,
}));
vi.mock('@/lib/compact', () => ({
  isCompacting: () => false,
  requestCompact: async () => null,
}));
// jsdom cannot navigate: the reload is the one thing replaced.
vi.mock('@/lib/buildCheck', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/buildCheck')>()),
  reloadPage: vi.fn(),
}));

const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');
const { reloadPage, BUILD_META_NAME } = await import('@/lib/buildCheck');
const { streamingIds } = await import('@/lib/streams');
const { clearAttachments } = await import('@/lib/attachments');
const { INLINE_IMAGE_BUDGET_BYTES } = await import('@/lib/orchestrator');

/* -------------------------------------------------------------- utilities */

/** An answer that streams one token and then waits until released. */
function heldStream(): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(enc.encode(`event: token\ndata: ${JSON.stringify({ text: 'The third is sharpest.' })}\n\n`));
      releaseStream = () => {
        releaseStream = null;
        try {
          c.enqueue(enc.encode('event: done\ndata: {}\n\n'));
          c.close();
        } catch {
          // Already closed by an abort.
        }
      };
    },
  });
}

function stubBrowser() {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => undefined);
  vi.stubGlobal('requestIdleCallback', (cb: () => void) => setTimeout(cb, 0));
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u === '/api/version') {
        versionReads += 1;
        return Response.json({ build: 'build-new' });
      }
      if (u === '/api/chat') {
        chatBodies.push(JSON.parse(String(init?.body)) as ChatRequestBody);
        return { ok: true, status: 200, body: heldStream() };
      }
      if (u === `/api/chat-media/${CONV}` && init?.method === 'POST') {
        const form = init.body as FormData;
        mediaPosts.push(form);
        await mediaGate;
        return Response.json({
          items: (form.getAll('attachment_id') as string[]).map((id) => ({
            attachment_id: id,
            mime: 'image/png',
            created: true,
          })),
        });
      }
      if (u === `/api/chat-media/${CONV}`) return Response.json({ items: [] });
      if (u === '/api/chat/active') return Response.json({ active: [] });
      return Response.json({});
    }),
  );
}

/** A PNG of `bytes` real bytes (PNG magic first, so the type is honest). */
function png(name: string, bytes: number): File {
  const data = new Uint8Array(Math.max(bytes, 8));
  data.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  return new File([data], name, { type: 'image/png' });
}

const box = () => screen.getByRole('textbox', { name: 'Message' }) as HTMLTextAreaElement;
const fileInput = () => document.querySelector('input[type="file"]') as HTMLInputElement;
const reloads = () => vi.mocked(reloadPage).mock.calls.length;
const userTurn = () => stored.find((m) => m.role === 'user')!;

function renderApp() {
  return render(
    <Providers>
      <ChatApp />
    </Providers>,
  );
}

async function attach(files: File[]) {
  await act(async () => {
    fireEvent.change(fileInput(), { target: { files } });
  });
  for (const file of files) {
    await screen.findByLabelText(`Remove attachment ${file.name}`, undefined, { timeout: 10_000 });
  }
}

/** The person comes back to the tab: the build check asks the server. */
async function cameBack() {
  await act(async () => {
    window.dispatchEvent(new Event('focus'));
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await act(async () => {
    await new Promise((r) => setTimeout(r, 10));
  });
}

async function wait(ms: number) {
  await act(async () => {
    await new Promise((r) => setTimeout(r, ms));
  });
}

beforeEach(() => {
  stored = [];
  chatBodies = [];
  mediaPosts = [];
  versionReads = 0;
  releaseStream = null;
  mediaGate = new Promise<void>((resolve) => {
    releaseMedia = resolve;
  });
  downscale.mockReset();
  downscale.mockImplementation(async () => null);
  vi.mocked(reloadPage).mockClear();
  clearAttachments();
  stubBrowser();
  window.history.replaceState(null, '', '/');
  window.sessionStorage.clear();
  document.head.innerHTML = '';
  // This page was served by the build the server has just replaced.
  const meta = document.createElement('meta');
  meta.name = BUILD_META_NAME;
  meta.content = 'build-old';
  document.head.append(meta);
});
afterEach(async () => {
  releaseMedia();
  releaseStream?.();
  await waitFor(() => expect(streamingIds()).toEqual([]), { timeout: 4000 });
  cleanup();
  vi.unstubAllGlobals();
  document.head.innerHTML = '';
});

/* ================================================================= tests */

describe('a deploy while an over-budget send stores its photos', () => {
  it('waits: the photos land, /chat names them, and the tab reloads only after the answer', async () => {
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    // Six photos this browser cannot shrink, 9 MiB each: 72 MiB of base64,
    // over the inline budget, so they are stored before /chat.
    const photos = Array.from({ length: 6 }, (_, i) => png(`big${i}.png`, 9 * MB));
    expect(6 * 9 * MB * (4 / 3)).toBeGreaterThan(INLINE_IMAGE_BUDGET_BYTES);
    await attach(photos);
    await act(async () => {
      fireEvent.change(box(), { target: { value: 'which of these is sharpest?' } });
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    });

    // The first batch is on its way and held. Nothing is typed or attached
    // any more, and /chat has not gone out: only the send's stream is live.
    await waitFor(() => expect(mediaPosts).toHaveLength(1), { timeout: 10_000 });
    expect(box().value).toBe('');
    expect(chatBodies).toHaveLength(0);
    expect(streamingIds()).toEqual([CONV]);

    // The deploy is noticed now: the banner, never the reload.
    await cameBack();
    expect(await screen.findByTestId('new-version-banner', undefined, { timeout: 4000 })).toBeTruthy();
    expect(versionReads).toBe(1);
    await wait(1_200);
    expect(reloads()).toBe(0);

    // The photos land; the turn goes out by reference, every photo named.
    await act(async () => {
      releaseMedia();
    });
    await waitFor(() => expect(chatBodies).toHaveLength(1), { timeout: 10_000 });
    const ids = userTurn().meta!.images!.map((image) => image.attachment_id);
    expect(ids).toHaveLength(6);
    expect(mediaPosts.flatMap((form) => form.getAll('attachment_id') as string[])).toEqual(ids);
    expect(chatBodies[0].image_refs).toEqual(ids);
    expect(chatBodies[0].images).toBeUndefined();
    expect(chatBodies[0].image).toBeUndefined();
    await screen.findByText('The third is sharpest.', undefined, { timeout: 4000 });

    // Streaming, composer empty: still not.
    await wait(1_200);
    expect(reloads()).toBe(0);

    // The answer is over: now it reloads, once.
    await act(async () => {
      releaseStream?.();
    });
    await waitFor(() => expect(streamingIds()).toEqual([]), { timeout: 4000 });
    await waitFor(() => expect(reloads()).toBe(1), { timeout: 6000 });
  }, 30_000);
});
