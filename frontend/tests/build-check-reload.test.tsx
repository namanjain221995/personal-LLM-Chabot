// @vitest-environment jsdom
/**
 * A chat tab running an older build than the server reloads
 * (docs/chat-media/STORE-ALWAYS.md §3, 2026-10-03), through the REAL ChatApp
 * and Composer, with only the network, the history store and the reload
 * itself stubbed.
 *
 * Production: a phone tab opened before the deploy kept its old JavaScript,
 * and the photo it sent 14 minutes later was never stored. The rules pinned:
 *   · equal ids do nothing;
 *   · a new build with an empty composer and nothing streaming reloads at once;
 *   · with a draft (text or an attachment) it shows the banner, keeps the
 *     draft, and reloads by itself once the send has finished and the
 *     composer is empty — never during the stream;
 *   · the banner's Reload keeps the typed text across the reload;
 *   · one request at a time, none while hidden, failures silent.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '@/lib/types';

/* ------------------------------------------------------------------ state */

let stored: ChatMessage[] = [];
let versionAnswer: () => Response = () => Response.json({ build: 'build-old' });
let versionReads = 0;
let chatBodies = 0;
let releaseStream: (() => void) | null = null;
const flush = vi.fn(async () => undefined);

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
    get: () => null,
    create: (title: string) => ({ id: 'conv-1', title, messages: [], createdAt: 0, updatedAt: 0 }),
    saveMessages: (_id: string, msgs: ChatMessage[]) => {
      stored = msgs;
    },
    flush,
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
const { reloadPage, RELOAD_DRAFT_KEY, RELOADED_FOR_KEY, BUILD_META_NAME } = await import(
  '@/lib/buildCheck'
);
const { streamingIds } = await import('@/lib/streams');
const { clearAttachments } = await import('@/lib/attachments');

/* -------------------------------------------------------------- utilities */

/** An answer that streams one token and then waits until released. */
function heldStream(): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(enc.encode(`event: token\ndata: ${JSON.stringify({ text: 'The total is 42.' })}\n\n`));
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
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const u = String(url);
      if (u === '/api/version') {
        versionReads += 1;
        return versionAnswer();
      }
      if (u === '/api/chat') {
        chatBodies += 1;
        return { ok: true, status: 200, body: heldStream() };
      }
      if (u === '/api/chat/active') return Response.json({ active: [] });
      return Response.json({});
    }),
  );
}

function servedBy(build: string) {
  const meta = document.createElement('meta');
  meta.name = BUILD_META_NAME;
  meta.content = build;
  document.head.append(meta);
}

const box = () => screen.getByRole('textbox', { name: 'Message' }) as HTMLTextAreaElement;
const reloads = () => vi.mocked(reloadPage).mock.calls.length;

function renderApp() {
  return render(
    <Providers>
      <ChatApp />
    </Providers>,
  );
}

async function cameBack() {
  await act(async () => {
    window.dispatchEvent(new Event('focus'));
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await act(async () => {
    await new Promise((r) => setTimeout(r, 10));
  });
}

async function type(text: string) {
  await act(async () => {
    fireEvent.change(box(), { target: { value: text } });
  });
}

beforeEach(() => {
  stored = [];
  versionReads = 0;
  chatBodies = 0;
  releaseStream = null;
  versionAnswer = () => Response.json({ build: 'build-old' });
  flush.mockClear();
  vi.mocked(reloadPage).mockClear();
  clearAttachments();
  stubBrowser();
  window.history.replaceState(null, '', '/');
  window.sessionStorage.clear();
  document.head.innerHTML = '';
});
afterEach(async () => {
  releaseStream?.();
  await waitFor(() => expect(streamingIds()).toEqual([]), { timeout: 4000 });
  cleanup();
  vi.unstubAllGlobals();
  document.head.innerHTML = '';
});

/* ================================================================= tests */

describe('a tab whose build is the server’s', () => {
  it('does nothing: no banner, no reload', async () => {
    servedBy('build-old');
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    await cameBack();
    expect(versionReads).toBe(1);
    expect(screen.queryByTestId('new-version-banner')).toBeNull();
    expect(reloads()).toBe(0);
  });

  it('never asks without a build id of its own (dev, an older server)', async () => {
    versionAnswer = () => Response.json({ build: 'build-new' });
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    await cameBack();
    expect(versionReads).toBe(0);
    expect(reloads()).toBe(0);
  });
});

describe('a tab left behind by a deploy', () => {
  beforeEach(() => {
    servedBy('build-old');
    versionAnswer = () => Response.json({ build: 'build-new' });
  });

  it('reloads at once when the composer is empty and nothing streams', async () => {
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    await cameBack();
    await waitFor(() => expect(reloads()).toBe(1));
    // The store was asked to finish its pushes first.
    expect(flush).toHaveBeenCalled();
    // A focus and a visibilitychange together are one request.
    expect(versionReads).toBe(1);
  });

  it('with typed text: the banner, the text kept, no reload', async () => {
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    await type('what is the due date on');
    await cameBack();
    expect(await screen.findByTestId('new-version-banner')).toBeTruthy();
    expect(screen.getByText('A new version is available')).toBeTruthy();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 1_200));
    });
    expect(reloads()).toBe(0);
    expect(box().value).toBe('what is the due date on');
  });

  it('with only an attachment: the banner, no reload', async () => {
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, {
        target: { files: [new File(['x'], 'invoice.png', { type: 'image/png' })] },
      });
    });
    await screen.findByLabelText('Remove attachment invoice.png');
    await cameBack();
    expect(await screen.findByTestId('new-version-banner')).toBeTruthy();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 1_200));
    });
    expect(reloads()).toBe(0);
  });

  it('reloads by itself once the send has finished and the composer is empty', async () => {
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    await type('what is the total?');
    await cameBack();
    await screen.findByTestId('new-version-banner');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    });
    await waitFor(() => expect(chatBodies).toBe(1));
    await screen.findByText('The total is 42.', undefined, { timeout: 4000 });
    // Streaming, composer empty: still not.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 1_200));
    });
    expect(reloads()).toBe(0);

    await act(async () => {
      releaseStream?.();
    });
    await waitFor(() => expect(streamingIds()).toEqual([]), { timeout: 4000 });
    await waitFor(() => expect(reloads()).toBe(1), { timeout: 3000 });
    expect(stored.some((m) => m.role === 'user' && m.content === 'what is the total?')).toBe(true);
  });

  it('the banner’s Reload keeps the typed text across the reload', async () => {
    window.history.replaceState(null, '', '/');
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    await type('half a question about the invoice');
    await cameBack();
    await screen.findByTestId('new-version-banner');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    });
    await waitFor(() => expect(reloads()).toBe(1));
    expect(window.sessionStorage.getItem(RELOAD_DRAFT_KEY)).toContain(
      'half a question about the invoice',
    );

    // The reload: a new page on the same address, on the new build.
    cleanup();
    document.head.innerHTML = '';
    servedBy('build-new');
    renderApp();
    await waitFor(() => expect(box().value).toBe('half a question about the invoice'));
    expect(window.sessionStorage.getItem(RELOAD_DRAFT_KEY)).toBeNull();
  });

  it('reloads by itself at most once per server build: no loop behind a cache', async () => {
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    await cameBack();
    await waitFor(() => expect(reloads()).toBe(1));
    expect(window.sessionStorage.getItem(RELOADED_FOR_KEY)).toBe('build-new');

    // The page that came back still carries the OLD id (a cache in between).
    cleanup();
    document.head.innerHTML = '';
    servedBy('build-old');
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    await cameBack();
    expect(await screen.findByTestId('new-version-banner')).toBeTruthy();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 1_200));
    });
    expect(reloads()).toBe(1);
    // The person can still ask for it.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    });
    await waitFor(() => expect(reloads()).toBe(2));
  });

  it('a page that IS the build it reloaded for clears the guard', async () => {
    window.sessionStorage.setItem(RELOADED_FOR_KEY, 'build-new');
    document.head.innerHTML = '';
    servedBy('build-new');
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    expect(window.sessionStorage.getItem(RELOADED_FOR_KEY)).toBeNull();
  });

  it('asks nothing while hidden, and a failed answer is silent', async () => {
    versionAnswer = () => new Response('down', { status: 502 });
    renderApp();
    await screen.findByRole('textbox', { name: 'Message' });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    try {
      await act(async () => {
        document.dispatchEvent(new Event('visibilitychange'));
      });
      expect(versionReads).toBe(0);
    } finally {
      delete (document as { hidden?: boolean }).hidden;
    }
    await cameBack();
    expect(versionReads).toBe(1);
    expect(screen.queryByTestId('new-version-banner')).toBeNull();
    expect(reloads()).toBe(0);
  });
});
