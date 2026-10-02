// @vitest-environment jsdom
/**
 * Chat media through the REAL ChatApp and the REAL startStream, with only the
 * network and the history store stubbed (2026-10-02,
 * docs/chat-media/CONTRACT.md §10). The assertions are on the JSON actually
 * posted to /api/chat and on the thread actually saved.
 *
 *   · Send: the turn gets `meta.images`, the body `image_ids` beside the
 *     bytes — the same ids, in the same order.
 *   · RC-3a: a document attached AFTER a photo keeps its chip (`pdfName`),
 *     its `meta.attachments` entry and its inline bytes. It used to vanish:
 *     everything was decided from `attachments[0]`, which was the photo.
 *   · RC-3c: the background document upload, landing after the answer,
 *     patches the LATEST stored thread — it used to re-save the list
 *     captured at send, shrinking the thread and losing the id.
 *   · Resend on a device without the bytes sends `image_refs`; a 422
 *     `image_ref_missing` shows the existing re-attach notice.
 *   · The legacy note, decided by the host from the answer that follows.
 *   · Opening a chat backfills photos this browser still holds.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { webcrypto } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatRequestBody } from '@/lib/orchestrator';
import type { ChatMessage } from '@/lib/types';

/* ------------------------------------------------------------------ state */

const CONV = 'conv-1';
let stored: ChatMessage[] = [];
let seeded: ChatMessage[] | null = null;
let chatBodies: ChatRequestBody[] = [];
let chatAnswer: () => Response | { ok: boolean; status: number; body: ReadableStream<Uint8Array> };
let mediaPosts: FormData[] = [];
let releaseUpload: ((value: { upload_id: string; name: string }) => void) | null = null;
const uploadDocumentFile = vi.fn(
  () =>
    new Promise<{ upload_id: string; name: string }>((resolve) => {
      releaseUpload = resolve;
    }),
);

vi.mock('@/lib/history', () => ({
  newId: () => `m${Math.random().toString(36).slice(2, 10)}`,
  setEvictListener: () => undefined,
  rebuildHistoryStore: async () => {
    throw new Error('unexpected account switch in test');
  },
  getHistoryStore: () => ({
    ready: async () => undefined,
    list: () => (seeded ? [{ id: CONV, title: 'photos', createdAt: 0, updatedAt: 0 }] : []),
    listArchived: () => [],
    get: (id: string) =>
      id === CONV && (stored.length || seeded)
        ? { id, title: 'photos', createdAt: 0, updatedAt: 0, messages: stored.length ? stored : seeded! }
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
vi.mock('@/lib/uploadDocument', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/uploadDocument')>()),
  uploadDocumentFile: () => uploadDocumentFile(),
}));

const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');
const { clearAttachments } = await import('@/lib/attachments');

/* -------------------------------------------------------------- utilities */

function sse(text: string): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(c) {
      const enc = new TextEncoder();
      c.enqueue(enc.encode(`event: token\ndata: ${JSON.stringify({ text })}\n\n`));
      c.enqueue(enc.encode('event: done\ndata: {}\n\n'));
      c.close();
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
  HTMLMediaElement.prototype.play = async () => undefined;
  HTMLMediaElement.prototype.pause = () => undefined;
  // The backfill hashes with WebCrypto, which jsdom does not carry.
  if (!globalThis.crypto?.subtle) vi.stubGlobal('crypto', webcrypto);
  // Idle time arrives at once in a test.
  vi.stubGlobal('requestIdleCallback', (cb: () => void) => setTimeout(cb, 0));
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u === '/api/chat') {
        chatBodies.push(JSON.parse(String(init?.body)) as ChatRequestBody);
        return chatAnswer();
      }
      if (u === `/api/chat-media/${CONV}` && init?.method === 'POST') {
        const form = init.body as FormData;
        mediaPosts.push(form);
        return Response.json({
          items: (form.getAll('attachment_id') as string[]).map((id) => ({
            attachment_id: id,
            mime: 'image/png',
            width: 640,
            height: 480,
            created: true,
          })),
        });
      }
      if (u === '/api/chat/active') return Response.json({ active: [] });
      return Response.json({});
    }),
  );
}

const png = (name: string) => new File(['x'], name, { type: 'image/png' });
const pdf = (name: string) => new File([`%PDF-1.4 ${name}`], name, { type: 'application/pdf' });
const box = () => screen.getByRole('textbox', { name: 'Message' });
const lastBody = () => chatBodies[chatBodies.length - 1];
const userTurn = () => stored.find((m) => m.role === 'user')!;

async function attach(files: File[]) {
  for (const file of files) {
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });
    await screen.findByLabelText(`Remove attachment ${file.name}`);
  }
}

async function send(text: string) {
  const before = chatBodies.length;
  await act(async () => {
    if (text) fireEvent.change(box(), { target: { value: text } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  });
  await waitFor(() => expect(chatBodies.length).toBe(before + 1), { timeout: 4000 });
}

async function answersOnScreen(n: number) {
  await waitFor(
    () =>
      expect(document.querySelectorAll('[data-chat-message-role="assistant"]').length).toBe(n),
    { timeout: 4000 },
  );
}

function renderApp() {
  return render(
    <Providers>
      <ChatApp />
    </Providers>,
  );
}

beforeEach(() => {
  stored = [];
  seeded = null;
  chatBodies = [];
  mediaPosts = [];
  releaseUpload = null;
  uploadDocumentFile.mockClear();
  chatAnswer = () => ({ ok: true, status: 200, body: sse('Here is the answer.') });
  clearAttachments();
  stubBrowser();
  window.history.replaceState(null, '', '/');
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/* ================================================================== send */

describe('sending a photo', () => {
  it('writes meta.images and sends image_ids — the same id, in order', async () => {
    renderApp();
    await attach([png('cat.png'), png('dog.png')]);
    await send('are these healthy?');

    const meta = userTurn().meta!;
    expect(meta.images?.map((i) => i.name)).toEqual(['cat.png', 'dog.png']);
    expect(meta.images?.every((i) => i.mime === 'image/png')).toBe(true);
    const ids = meta.images!.map((i) => i.attachment_id);
    expect(lastBody().image_ids).toEqual(ids);
    expect(lastBody().images).toHaveLength(2);
    // A photo-only turn gains the reference and nothing else.
    expect(meta.attachments).toBeUndefined();
    expect(userTurn().pdfName).toBeUndefined();
  });
});

describe('RC-3a · a document attached AFTER a photo', () => {
  it('keeps its chip, its meta.attachments entry and its inline bytes', async () => {
    renderApp();
    await attach([png('chart.png'), pdf('report.pdf')]);
    await send('compare the chart to the report');

    const turn = userTurn();
    expect(turn.pdfName).toBe('report.pdf');
    expect(turn.meta?.attachments).toEqual([
      expect.objectContaining({ name: 'report.pdf', kind: 'pdf', upload_state: 'selected' }),
    ]);
    expect(turn.meta?.images?.map((i) => i.name)).toEqual(['chart.png']);

    const body = lastBody();
    expect(body.pdf).toBeTruthy();
    expect(body.pdf_filename).toBe('report.pdf');
    expect(body.image).toBeTruthy();
    expect(body.image_ids).toEqual([turn.meta!.images![0].attachment_id]);
    // Both cards are on screen: the photo, and the document's chip.
    expect(screen.getByRole('button', { name: /report\.pdf — preview/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /chart\.png — preview/ })).toBeTruthy();
  });
});

describe('RC-3c · the background document upload lands after the answer', () => {
  it('patches the LATEST stored thread by attachment id — nothing shrinks, the id stays', async () => {
    renderApp();
    await attach([pdf('report.pdf')]);
    await send('summarise this');
    await answersOnScreen(1);
    // The answer is stored; the upload has not landed yet.
    await waitFor(() => expect(stored.some((m) => m.role === 'assistant')).toBe(true));
    expect(uploadDocumentFile).toHaveBeenCalledTimes(1);
    const before = stored.length;

    await act(async () => {
      releaseUpload!({ upload_id: 'f'.repeat(32), name: 'report.pdf' });
    });

    await waitFor(() =>
      expect(userTurn().meta?.attachments?.[0]).toEqual(
        expect.objectContaining({ id: 'f'.repeat(32), upload_state: 'uploaded' }),
      ),
    );
    // The save carried the whole thread, answer included.
    expect(stored.length).toBe(before);
    expect(stored.some((m) => m.role === 'assistant' && m.content === 'Here is the answer.')).toBe(true);
  });
});

/* ======================================================= another device */

function seedThread(thread: ChatMessage[]) {
  seeded = thread;
  window.history.replaceState(null, '', `/?c=${CONV}`);
}

const askedWithPhoto: ChatMessage = {
  id: 'srv-conv-1-0',
  role: 'user',
  content: 'is this leaf healthy?',
  meta: { images: [{ attachment_id: 'img-aaaa-0001', name: 'leaf.jpg', width: 800, height: 600 }] },
  createdAt: 1,
};
const visionAnswer: ChatMessage = {
  id: 'srv-conv-1-1',
  role: 'assistant',
  content: 'It looks healthy.',
  meta: { route: 'vision' },
  status: 'done',
  createdAt: 2,
};

describe('a device that never held the photo', () => {
  it('shows the stored thumbnail', async () => {
    seedThread([askedWithPhoto, visionAnswer]);
    const { container } = renderApp();
    await waitFor(() =>
      expect(container.querySelector('img[data-testid="stored-image"]')?.getAttribute('src')).toBe(
        `/api/chat-media/${CONV}/img-aaaa-0001?size=thumb`,
      ),
    );
  });

  it('regenerates by reference (image_refs), never silently without the photo', async () => {
    seedThread([askedWithPhoto, visionAnswer]);
    renderApp();
    await screen.findByText('It looks healthy.');
    const buttons = await screen.findAllByRole('button', { name: /Try again/i });
    await act(async () => {
      fireEvent.click(buttons[buttons.length - 1]);
    });
    await waitFor(() => expect(chatBodies).toHaveLength(1));
    expect(lastBody().image_refs).toEqual(['img-aaaa-0001']);
    expect(lastBody()).not.toHaveProperty('image');
  });

  it('a 422 image_ref_missing shows the existing re-attach notice', async () => {
    chatAnswer = () =>
      Response.json({ code: 'image_ref_missing', missing: ['img-aaaa-0001'] }, { status: 422 });
    seedThread([askedWithPhoto, visionAnswer]);
    renderApp();
    await screen.findByText('It looks healthy.');
    const buttons = await screen.findAllByRole('button', { name: /Try again/i });
    await act(async () => {
      fireEvent.click(buttons[buttons.length - 1]);
    });
    expect(
      await screen.findByText(
        'Re-attach the file to regenerate this answer — its contents are no longer in memory.',
      ),
    ).toBeTruthy();
    // Withdrawn, not failed: no error row, and the answer is still there.
    expect(screen.queryByText(/Something went wrong/)).toBeNull();
    expect(screen.getByText('It looks healthy.')).toBeTruthy();
  });

  it('a photo sent before photos were stored says so — once, under a vision answer', async () => {
    seedThread([
      { id: 'srv-conv-1-0', role: 'user', content: 'is this leaf healthy?', createdAt: 1 },
      visionAnswer,
    ]);
    renderApp();
    expect(await screen.findByTestId('legacy-photo-note')).toBeTruthy();
    expect(screen.getAllByTestId('legacy-photo-note')).toHaveLength(1);
  });
});

/* ============================================================ backfill */

describe('the browser that still holds an old photo', () => {
  it('backfills it on open: uploads with a bf- id and writes meta.images', async () => {
    const PNG = 'data:image/png;base64,iVBORw0KGgo=';
    seedThread([
      { id: 'srv-conv-1-0', role: 'user', content: 'is this leaf healthy?', imageDataUrl: PNG, createdAt: 1 },
      visionAnswer,
    ]);
    renderApp();
    await waitFor(() => expect(mediaPosts).toHaveLength(1), { timeout: 4000 });
    const form = mediaPosts[0];
    expect(form.get('source')).toBe('backfill');
    const id = form.get('attachment_id') as string;
    expect(id).toMatch(/^bf-[0-9a-f]{32}$/);
    await waitFor(() =>
      expect(stored[0]?.meta?.images).toEqual([
        { attachment_id: id, mime: 'image/png', width: 640, height: 480 },
      ]),
    );
    // The photo on screen is still this browser's own copy.
    expect(document.querySelector(`img[src="${PNG}"]`)).toBeTruthy();
    // No legacy note: this device has the photo.
    expect(screen.queryByTestId('legacy-photo-note')).toBeNull();
  });
});
