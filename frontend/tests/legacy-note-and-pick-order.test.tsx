// @vitest-environment jsdom
/**
 * Two defects a real-browser run found (checks S7 and C6, 2026-10-03; both
 * also failed on production main 6da0e929), pinned here through the REAL
 * ChatApp, Composer and MessageRow with only the network, the history store,
 * the browser's photo shrink and the timing of file reads controlled.
 *
 *   · S7: a turn that never had a photo (5 or 50 documents) carried "Photo not
 *     stored on the server …" on both devices. The document engine answers
 *     under route 'vision' too (orchestrator engines/document.py, with
 *     `meta.document`), and the note read any photo-less turn under a vision
 *     answer as a photo sent before photos were stored.
 *   · C6: attachments landed in the order their reads FINISHED, not the order
 *     they were picked: mid.jpg, small.jpg, b100.jpg became small, b100, mid
 *     (a photo that needs shrinking is slower), the same for a document that
 *     takes longer to read, and with 100 photos the first two swapped (three
 *     decodes run at once). The chips, the bubble, the /chat body and
 *     `meta.images` / `meta.attachments` all follow the chips.
 *
 * Fixtures use low-entropy ids on purpose (memory note "gitleaks trips on
 * fabricated UUID fixtures").
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatRequestBody } from '@/lib/orchestrator';
import type { DownscaledImage } from '@/lib/images';
import type { ChatMessage } from '@/lib/types';

/* ------------------------------------------------------------------ state */

const CONV = 'conv-1';
const NOTE =
  'Photo not stored on the server (sent before photos were saved), so it only shows on the device that sent it.';
let stored: ChatMessage[] = [];
let chatBodies: ChatRequestBody[] = [];
let uploadCount = 0;
let mediaListReads = 0;
/** The browser's shrink, per file; jsdom has no canvas. */
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
        ? { id, title: 'order', createdAt: 0, updatedAt: 0, messages: stored }
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
  uploadDocumentFile: async (file: File) => {
    uploadCount += 1;
    return { upload_id: String(uploadCount).padStart(32, '0'), name: file.name };
  },
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

/**
 * File reads the test releases by hand: `hold(name)` parks that file's
 * FileReader.readAsDataURL until `release(name)`. Every other read runs as
 * the browser's would.
 */
const heldReads = new Map<string, Array<() => void>>();
const holding = new Set<string>();
function hold(name: string) {
  holding.add(name);
}
async function release(name: string) {
  holding.delete(name);
  const parked = heldReads.get(name) ?? [];
  heldReads.delete(name);
  await act(async () => {
    for (const start of parked) start();
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
  const RealFileReader = globalThis.FileReader;
  class HeldFileReader extends RealFileReader {
    readAsDataURL(blob: Blob) {
      const name = (blob as File).name;
      if (name && holding.has(name)) {
        const parked = heldReads.get(name) ?? [];
        parked.push(() => super.readAsDataURL(blob));
        heldReads.set(name, parked);
        return;
      }
      super.readAsDataURL(blob);
    }
  }
  vi.stubGlobal('FileReader', HeldFileReader);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u === '/api/chat') {
        chatBodies.push(JSON.parse(String(init?.body)) as ChatRequestBody);
        return { ok: true, status: 200, body: sse('Here is the answer.') };
      }
      if (u === `/api/chat-media/${CONV}` && init?.method === 'POST') {
        const form = init.body as FormData;
        return Response.json({
          items: (form.getAll('attachment_id') as string[]).map((id) => ({
            attachment_id: id,
            mime: 'image/jpeg',
            created: true,
          })),
        });
      }
      if (u === `/api/chat-media/${CONV}`) {
        mediaListReads += 1;
        // The server holds no photo for any turn of these chats.
        return Response.json({ items: [] });
      }
      if (u === '/api/chat/active') return Response.json({ active: [] });
      return Response.json({});
    }),
  );
}

/** A shrunk photo whose bytes name the file, so order can be read back off the wire. */
function shrunkTo(file: File): DownscaledImage {
  return {
    dataUrl: `data:image/jpeg;base64,${btoa(file.name)}`,
    width: 64,
    height: 48,
    mime: 'image/jpeg',
  };
}
/** The shrink, with the named photos left pending until their resolver is called. */
function slowShrink(...slow: string[]) {
  const waiting = new Map<string, () => void>();
  downscale.mockImplementation(
    (file: File) =>
      new Promise<DownscaledImage | null>((resolve) => {
        const done = () => resolve(shrunkTo(file));
        if (slow.includes(file.name)) waiting.set(file.name, done);
        else done();
      }),
  );
  return {
    async finish(name: string) {
      await waitFor(() => expect(waiting.has(name)).toBe(true));
      await act(async () => {
        waiting.get(name)!();
      });
    },
  };
}

const jpeg = (name: string) => new File([`jpeg ${name}`], name, { type: 'image/jpeg' });
const text = (name: string, body = `notes in ${name}`) =>
  new File([body], name, { type: name.endsWith('.md') ? 'text/markdown' : 'text/plain' });
const box = () => screen.getByRole('textbox', { name: 'Message' });
const fileInput = () => document.querySelector('input[type="file"]') as HTMLInputElement;
const lastBody = () => chatBodies[chatBodies.length - 1];
const userTurn = () => stored.find((m) => m.role === 'user')!;
const chipNames = () =>
  screen
    .queryAllByLabelText(/^Remove attachment /)
    .map((el) => el.getAttribute('aria-label')!.replace(/^Remove attachment /, ''));
const decoded = (base64s: string[] | undefined) => (base64s ?? []).map((b) => atob(b));

async function pick(files: File[]) {
  await act(async () => {
    fireEvent.change(fileInput(), { target: { files } });
  });
}

async function send(words: string) {
  const before = chatBodies.length;
  await act(async () => {
    fireEvent.change(box(), { target: { value: words } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  });
  await waitFor(() => expect(chatBodies.length).toBe(before + 1), { timeout: 10_000 });
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
  chatBodies = [];
  uploadCount = 0;
  mediaListReads = 0;
  heldReads.clear();
  holding.clear();
  downscale.mockReset();
  downscale.mockImplementation(async () => null);
  clearAttachments();
  stubBrowser();
  window.history.replaceState(null, '', '/');
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/* ===================================================== C6: the pick order */

describe('C6: attachments keep the order they were picked in', () => {
  it('photos: a slower shrink keeps its place in the chips, the bubble, /chat and meta.images', async () => {
    const picked = [jpeg('mid.jpg'), jpeg('small.jpg'), jpeg('b100.jpg')];
    const shrink = slowShrink('mid.jpg');
    renderApp();
    await pick(picked);
    // The two quick ones land first; the slow one must still go FIRST.
    await screen.findByLabelText('Remove attachment b100.jpg');
    await shrink.finish('mid.jpg');
    await screen.findByLabelText('Remove attachment mid.jpg');
    expect(chipNames()).toEqual(['mid.jpg', 'small.jpg', 'b100.jpg']);

    await send('which is sharpest?');
    const names = picked.map((f) => f.name);
    const body = lastBody();
    expect(decoded(body.images)).toEqual(names);
    expect(atob(body.image!)).toBe('mid.jpg');
    const turn = userTurn();
    expect(turn.meta?.images?.map((i) => i.name)).toEqual(names);
    expect(body.image_ids).toEqual(turn.meta?.images?.map((i) => i.attachment_id));
    // The sender's bubble draws the photos it holds, in the same order.
    const bubble = screen
      .getAllByAltText(/^Attached image \d+$/)
      .map((img) => atob(img.getAttribute('src')!.split(',')[1]));
    expect(bubble).toEqual(names);
  });

  it('the first photo of a big pick finishing second does not swap the first two', async () => {
    const picked = Array.from({ length: 12 }, (_, i) => jpeg(`p${i}.jpg`));
    const shrink = slowShrink('p0.jpg');
    renderApp();
    await pick(picked);
    await screen.findByLabelText('Remove attachment p11.jpg');
    await shrink.finish('p0.jpg');
    await screen.findByLabelText('Remove attachment p0.jpg');
    expect(chipNames()).toEqual(picked.map((f) => f.name));

    await send('sort these');
    expect(userTurn().meta?.images?.map((i) => i.name)).toEqual(picked.map((f) => f.name));
    expect(decoded(lastBody().images)).toEqual(picked.map((f) => f.name));
  });

  it('documents: a slower read keeps its place in the chips, /chat and meta.attachments', async () => {
    const picked = [text('order-big.txt', 'x'.repeat(4096)), text('bdoc-0.txt'), text('bdoc-1.md')];
    hold('order-big.txt');
    renderApp();
    await pick(picked);
    await screen.findByLabelText('Remove attachment bdoc-1.md');
    await screen.findByLabelText('Remove attachment bdoc-0.txt');
    await release('order-big.txt');
    await screen.findByLabelText('Remove attachment order-big.txt');
    const names = picked.map((f) => f.name);
    expect(chipNames()).toEqual(names);

    await send('compare these');
    expect(lastBody().pdf_uploads?.map((d) => d.name)).toEqual(names);
    expect(userTurn().meta?.attachments?.map((a) => a.name)).toEqual(names);
    expect(userTurn().pdfName).toBe('order-big.txt');
  });

  it('a slow photo picked before a file that streams keeps its place ahead of it', async () => {
    // A document over the inline size never waits on a read: its chip lands
    // the moment it is picked, which used to put it ahead of everything
    // picked before it that still had to be read.
    const big = text('report.txt');
    Object.defineProperty(big, 'size', { value: 30 * 1024 * 1024 });
    const shrink = slowShrink('first.jpg');
    renderApp();
    await pick([jpeg('first.jpg'), big]);
    await screen.findByLabelText('Remove attachment report.txt');
    await shrink.finish('first.jpg');
    await screen.findByLabelText('Remove attachment first.jpg');
    expect(chipNames()).toEqual(['first.jpg', 'report.txt']);
  });
});

/* ================================================ S7: the legacy photo note */

const INTENT_DOCS = '0'.repeat(31) + '1';
const INTENT_PHOTO = '0'.repeat(31) + '2';

/** A user turn as a current page writes it: five documents, no photo. */
function documentTurn(count = 5): ChatMessage {
  return {
    id: 'srv-docs-0',
    role: 'user',
    content: 'compare these documents',
    createdAt: 1,
    meta: {
      route: 'chat',
      attachments: Array.from({ length: count }, (_, i) => ({
        id: String(i + 1).padStart(32, '0'),
        name: `bdoc-${i}.txt`,
        kind: 'pdf' as const,
        attachment_id: `att-doc-${String(i).padStart(4, '0')}`,
        upload_state: 'uploaded' as const,
      })),
      intent: { id: INTENT_DOCS, state: 'completed' },
    },
  };
}
/** The document engine's answer: route 'vision', with what it read. */
function documentAnswer(words = 'The five documents agree on the totals.'): ChatMessage {
  return {
    id: 'srv-docs-1',
    role: 'assistant',
    content: words,
    createdAt: 2,
    meta: {
      route: 'vision',
      document: { filename: 'bdoc-0.txt (+4 more)', total_pages: 5, ocr_pages: 0, pages: [] },
    },
  };
}
/** A photo turn from a page too old to store photos, seen on another device. */
function oldPhotoTurn(): ChatMessage {
  return {
    id: 'srv-photo-0',
    role: 'user',
    content: 'is this leaf healthy?',
    createdAt: 3,
    meta: { intent: { id: INTENT_PHOTO, state: 'completed' } },
  };
}
function visionAnswer(): ChatMessage {
  return {
    id: 'srv-photo-1',
    role: 'assistant',
    content: 'The leaf looks healthy.',
    createdAt: 4,
    meta: { route: 'vision' },
  };
}

/** Open the stored chat and wait until its photo list has been read and applied. */
async function openStoredChat(messages: ChatMessage[], lastWords: string) {
  stored = messages;
  window.history.replaceState(null, '', `/?c=${CONV}`);
  renderApp();
  await screen.findByText(lastWords, undefined, { timeout: 4000 });
  await waitFor(() => expect(mediaListReads).toBeGreaterThan(0));
  // Let the list's answer reach state and the rows re-render.
  for (let i = 0; i < 5; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

describe('S7: the legacy photo line is for photos only', () => {
  it('a five-document turn under the document engine’s answer says nothing about a photo', async () => {
    await openStoredChat([documentTurn(5), documentAnswer()], 'The five documents agree on the totals.');
    expect(screen.queryByTestId('legacy-photo-note')).toBeNull();
    expect(screen.queryByText(NOTE)).toBeNull();
  });

  it('fifty documents, or a document the engine could not read, say nothing either', async () => {
    const refusal: ChatMessage = {
      id: 'srv-docs-1',
      role: 'assistant',
      content: 'That document has no readable content.',
      createdAt: 2,
      // engines/document.py's refusal: the route and nothing else.
      meta: { route: 'vision' },
    };
    await openStoredChat([documentTurn(50), refusal], 'That document has no readable content.');
    expect(screen.queryByText(NOTE)).toBeNull();
  });

  it('still names the old photo turn — and only it — in a chat that also read documents', async () => {
    await openStoredChat(
      [documentTurn(5), documentAnswer(), oldPhotoTurn(), visionAnswer()],
      'The leaf looks healthy.',
    );
    const notes = await screen.findAllByTestId('legacy-photo-note');
    expect(notes).toHaveLength(1);
    // The line sits in the photo turn's own row, above its question.
    const row = notes[0].closest('.group\\/msg') as HTMLElement;
    expect(row.querySelector('[data-chat-message-role="user"]')?.textContent).toBe(
      'is this leaf healthy?',
    );
  });

  it('keeps the true case: an old photo turn with no stored copy still says so', async () => {
    await openStoredChat([oldPhotoTurn(), visionAnswer()], 'The leaf looks healthy.');
    expect(await screen.findByText(NOTE)).toBeTruthy();
  });
});
