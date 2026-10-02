// @vitest-environment jsdom
/**
 * No upload limit (owner, 2026-10-03, docs/chat-media/LIMITS.md: "no limit,
 * users can upload unlimited"), through the REAL ChatApp, Composer and
 * startStream with only the network, the history store and the browser's
 * image shrink stubbed. Assertions are on what is actually posted and saved.
 *
 *   · Photo size is ruled on what is SENT: a 30 MB original the browser
 *     shrinks is accepted; one it cannot shrink keeps the server's 10 MB
 *     stored-file rule, in words that say why.
 *   · Any number of photos and files: 100 photos and 50 documents in one
 *     message, nothing refused, nothing said about a number.
 *   · No size limit on what streams: a document or dataset over 512 MB and a
 *     video over 4 GB attach and go by reference (datasets too take the
 *     chunked rail now).
 *   · A send whose inline photos would exceed INLINE_IMAGE_BUDGET_BYTES
 *     stores them first (POST /api/chat-media, batches under the budget) and
 *     names them in `image_refs`: no inline bytes, `meta.images` unchanged.
 *     Under the budget nothing changes: inline, and no extra round trip.
 *   · No string in the app still states an old limit.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatRequestBody } from '@/lib/orchestrator';
import { INLINE_IMAGE_BUDGET_BYTES, MAX_DOCUMENTS, MAX_IMAGES } from '@/lib/orchestrator';
import type { DownscaledImage } from '@/lib/images';
import type { ChatMessage } from '@/lib/types';

/* ------------------------------------------------------------------ state */

const CONV = 'conv-1';
let stored: ChatMessage[] = [];
let chatBodies: ChatRequestBody[] = [];
let mediaPosts: FormData[] = [];
let uploadCount = 0;
/** Every file that went by reference, and the rail it was given. */
let uploads: { name: string; size: number; purpose: string }[] = [];
/** Single-shot dataset posts to /api/upload. */
let datasetPosts = 0;
/** The status POST /api/chat-media answers; 200 stores. */
let mediaStatus = 200;
/** The browser's shrink. jsdom has no canvas, so `null` ("send as is") by default. */
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
        ? { id, title: 'limits', createdAt: 0, updatedAt: 0, messages: stored }
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
  uploadDocumentFile: async (file: File, _conversationId: string, purpose = 'document') => {
    uploadCount += 1;
    uploads.push({ name: file.name, size: file.size, purpose });
    return { upload_id: String(uploadCount).padStart(32, '0'), name: file.name };
  },
}));

const { ChatApp } = await import('@/components/ChatApp');
const { MessageRow } = await import('@/components/MessageRow');
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
  vi.stubGlobal('requestIdleCallback', (cb: () => void) => setTimeout(cb, 0));
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
        mediaPosts.push(form);
        if (mediaStatus !== 200) return Response.json({}, { status: mediaStatus });
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
      if (u === '/api/upload') {
        datasetPosts += 1;
        return Response.json({ upload_id: 'd'.repeat(32), files: 1 });
      }
      return Response.json({});
    }),
  );
}

/** A PNG of `bytes` real bytes (PNG magic first, so the type is honest). */
function png(name: string, bytes = 1): File {
  const data = new Uint8Array(Math.max(bytes, 8));
  data.set([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  return new File([data], name, { type: 'image/png' });
}
const pdf = (name: string) => new File([`%PDF-1.4 ${name}`], name, { type: 'application/pdf' });
/** A file that claims `size` bytes without holding them. */
function sized(file: File, size: number): File {
  Object.defineProperty(file, 'size', { value: size });
  return file;
}
const MB = 1024 * 1024;
const GB = 1024 * MB;
/** Anything a toast could say about a count or a size cap. */
const LIMIT_WORDS = /up to \d+|can carry|left out|the limit is|at most|512 MB|4 GB/;
const box = () => screen.getByRole('textbox', { name: 'Message' });
const fileInput = () => document.querySelector('input[type="file"]') as HTMLInputElement;
const lastBody = () => chatBodies[chatBodies.length - 1];
const userTurn = () => stored.find((m) => m.role === 'user')!;
const chips = () => screen.queryAllByLabelText(/^Remove attachment /);

async function pick(files: File[]) {
  await act(async () => {
    fireEvent.change(fileInput(), { target: { files } });
  });
}

async function attach(files: File[]) {
  await pick(files);
  for (const file of files) await screen.findByLabelText(`Remove attachment ${file.name}`);
}

async function send(text: string) {
  const before = chatBodies.length;
  await act(async () => {
    if (text) fireEvent.change(box(), { target: { value: text } });
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
  mediaPosts = [];
  uploadCount = 0;
  uploads = [];
  datasetPosts = 0;
  mediaStatus = 200;
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

/* ============================================================ photo size */

describe('the photo size rule is on what is sent', () => {
  it('accepts a 30 MB original that the browser shrinks, and sends the shrunk bytes', async () => {
    const shrunk = 'data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ';
    downscale.mockImplementation(async () => ({
      dataUrl: shrunk,
      width: 1600,
      height: 1200,
      mime: 'image/jpeg',
    }));
    const phone = new File([new Uint8Array(30 * 1024 * 1024)], 'phone.jpg', {
      type: 'image/jpeg',
    });
    renderApp();
    await attach([phone]);
    expect(screen.queryByText(/at most 10 MB|the limit is 10 MB/)).toBeNull();

    await send('what is this plant?');
    expect(lastBody().image).toBe(shrunk.slice(shrunk.indexOf(',') + 1));
    expect(userTurn().meta?.images).toEqual([
      expect.objectContaining({ name: 'phone.jpg', mime: 'image/jpeg', width: 1600, height: 1200 }),
    ]);
  });

  it('refuses an 11 MB photo the browser cannot shrink, and says why', async () => {
    renderApp();
    await pick([png('scan.heic.png', 11 * 1024 * 1024)]);
    expect(
      await screen.findByText(
        'scan.heic.png is 11.0 MB and this browser couldn’t make it smaller. A photo sent as it is can be at most 10 MB.',
      ),
    ).toBeTruthy();
    expect(chips()).toHaveLength(0);
  });
});

/* ========================================================= no count limit */

describe('any number of photos and files in one message', () => {
  it('100 photos: all attached, stored by reference in batches, all named on the turn', async () => {
    expect(MAX_IMAGES).toBe(999);
    // 600 KB each, sent as they are (jsdom cannot shrink): 60 MB of bytes is
    // 80 MB of base64, over the 48 MiB inline budget.
    const photos = Array.from({ length: 100 }, (_, i) => png(`p${i}.png`, 600 * 1024));
    renderApp();
    await attach(photos);
    expect(chips()).toHaveLength(100);
    expect(screen.queryByText(LIMIT_WORDS)).toBeNull();

    await send('sort these by date');
    const turn = userTurn();
    const ids = turn.meta!.images!.map((i) => i.attachment_id);
    expect(turn.meta!.images!.map((i) => i.name)).toEqual(photos.map((p) => p.name));
    expect(new Set(ids).size).toBe(100);

    // Every photo stored once, in order, in requests of at most the
    // budget's bytes: 81 photos of 600 KB fit 48 MiB, the other 19 follow.
    expect(mediaPosts.map((f) => f.getAll('file').length)).toEqual([81, 19]);
    const posted = mediaPosts.flatMap((f) => f.getAll('attachment_id') as string[]);
    expect(posted).toEqual(ids);
    for (const form of mediaPosts) {
      const bytes = (form.getAll('file') as File[]).reduce((n, f) => n + f.size, 0);
      expect(bytes).toBeLessThanOrEqual(INLINE_IMAGE_BUDGET_BYTES);
    }
    const body = lastBody();
    expect(body.image_refs).toEqual(ids);
    expect(body.image).toBeUndefined();
    expect(body.images).toBeUndefined();
    expect(JSON.stringify(body).length).toBeLessThan(64 * 1024);
  }, 60_000);

  it('100 stored photos all show on a second device', () => {
    const images = Array.from({ length: 100 }, (_, i) => ({
      attachment_id: `img-many-${String(i).padStart(4, '0')}`,
      name: `p${i}.jpg`,
      width: 1600,
      height: 1200,
    }));
    render(
      <MessageRow
        message={{ id: 'u1', role: 'user', content: 'sort these', createdAt: 0, meta: { images } }}
        isLast={false}
        onRegenerate={vi.fn()}
        onRetry={vi.fn()}
        conversationId={CONV}
      />,
    );
    const shown = screen.getAllByTestId('stored-image') as HTMLImageElement[];
    expect(shown).toHaveLength(100);
    expect(shown[99].getAttribute('src')).toContain('img-many-0099');
  });

  it('50 documents in one message, each by reference, nothing said about a number', async () => {
    expect(MAX_DOCUMENTS).toBe(999);
    renderApp();
    const docs = Array.from({ length: 50 }, (_, i) => pdf(`d${i}.pdf`));
    await attach(docs);
    expect(chips()).toHaveLength(50);
    expect(screen.queryByText(LIMIT_WORDS)).toBeNull();

    await send('compare all of these');
    expect(lastBody().pdf_uploads?.map((d) => d.name)).toEqual(docs.map((d) => d.name));
    expect(userTurn().meta?.attachments).toHaveLength(50);
  }, 30_000);
});

/* ========================================================== no size limit */

describe('no size limit on what streams', () => {
  it('a 600 MB document attaches and goes by reference', async () => {
    renderApp();
    const big = sized(pdf('archive-scan.pdf'), 600 * MB);
    await attach([big]);
    expect(screen.queryByText(LIMIT_WORDS)).toBeNull();
    await send('summarise this');
    expect(lastBody().pdf_uploads?.map((d) => d.name)).toEqual(['archive-scan.pdf']);
    expect(uploads).toContainEqual({ name: 'archive-scan.pdf', size: 600 * MB, purpose: 'document' });
  });

  it('a video over 4 GB attaches and goes by reference', async () => {
    renderApp();
    const film = sized(new File(['x'], 'all-day.mp4', { type: 'video/mp4' }), 5 * GB);
    await attach([film]);
    expect(screen.queryByText(LIMIT_WORDS)).toBeNull();
    await send('what happens in this?');
    expect(lastBody().video_uploads?.map((d) => d.name)).toEqual(['all-day.mp4']);
    expect(uploads).toContainEqual({ name: 'all-day.mp4', size: 5 * GB, purpose: 'video' });
  });

  it('a 600 MB dataset attaches and takes the chunked rail', async () => {
    renderApp();
    await attach([sized(new File(['a,b\n1,2\n'], 'events.csv', { type: 'text/csv' }), 600 * MB)]);
    expect(screen.queryByText(LIMIT_WORDS)).toBeNull();
    await send('how many rows?');
    expect(uploads).toEqual([{ name: 'events.csv', size: 600 * MB, purpose: 'dataset' }]);
    expect(datasetPosts).toBe(0);
    expect(lastBody().dataset).toBe(true);
  });

  it('a small dataset still posts once to /api/upload, as before', async () => {
    renderApp();
    await attach([new File(['a,b\n1,2\n'], 'small.csv', { type: 'text/csv' })]);
    await send('how many rows?');
    expect(uploads).toEqual([]);
    expect(datasetPosts).toBe(1);
  });
});

/* ======================================================= the inline budget */

describe('the inline payload budget', () => {
  it('under the budget: the photos ride inline, with no extra round trip', async () => {
    renderApp();
    await attach([png('a.png', 2048), png('b.png', 2048)]);
    await send('compare');

    const body = lastBody();
    const ids = userTurn().meta!.images!.map((i) => i.attachment_id);
    expect(body.images).toHaveLength(2);
    expect(body.image_ids).toEqual(ids);
    expect(body.image_refs).toBeUndefined();
    expect(mediaPosts).toHaveLength(0);
  });

  it('over the budget: the photos are stored first, in batches, and the turn names them', async () => {
    // Six photos this browser cannot shrink, 9 MiB each: 54 MiB of bytes is
    // 72 MiB of base64, over the 48 MiB budget (and 20 such photos would be
    // 240 MiB — past Cloudflare's 100 MB and the proxy's 128 MiB).
    const photos = Array.from({ length: 6 }, (_, i) => png(`big${i}.png`, 9 * 1024 * 1024));
    renderApp();
    await attach(photos);
    await send('which of these is sharpest?');

    const turn = userTurn();
    const ids = turn.meta!.images!.map((i) => i.attachment_id);
    // meta.images is exactly what an inline send writes: every photo, in
    // order, by name and the type the bytes were sent as.
    expect(turn.meta!.images!.map((i) => i.name)).toEqual(photos.map((p) => p.name));
    expect(turn.meta!.images!.every((i) => i.mime === 'image/png')).toBe(true);

    // Stored first, in requests that each stay under the budget, in order.
    expect(mediaPosts.length).toBeGreaterThan(1);
    const posted = mediaPosts.flatMap((f) => f.getAll('attachment_id') as string[]);
    expect(posted).toEqual(ids);
    for (const form of mediaPosts) {
      const bytes = (form.getAll('file') as File[]).reduce((n, f) => n + f.size, 0);
      expect(bytes).toBeLessThanOrEqual(INLINE_IMAGE_BUDGET_BYTES);
      expect(form.get('source')).toBe('upload');
    }
    const firstFile = mediaPosts[0].getAll('file')[0] as File;
    expect(firstFile.size).toBe(9 * 1024 * 1024);
    expect(firstFile.type).toBe('image/png');

    // The request names them and carries no photo bytes at all.
    const body = lastBody();
    expect(body.image_refs).toEqual(ids);
    expect(body.image).toBeUndefined();
    expect(body.images).toBeUndefined();
    expect(body.image_ids).toBeUndefined();
    expect(JSON.stringify(body).length).toBeLessThan(64 * 1024);
  }, 30_000);

  it('over the budget, a store the server refuses sends nothing and says so on the turn', async () => {
    mediaStatus = 507;
    const photos = Array.from({ length: 6 }, (_, i) => png(`big${i}.png`, 9 * 1024 * 1024));
    renderApp();
    await attach(photos);
    await act(async () => {
      fireEvent.change(box(), { target: { value: 'which is sharpest?' } });
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    });
    await waitFor(() => expect(userTurn().meta?.intent?.state).toBe('unsent'), { timeout: 10_000 });
    expect(mediaPosts).toHaveLength(1);
    expect(chatBodies).toHaveLength(0);
    // The references stay: a retry stores the same ids and sends them.
    expect(userTurn().meta?.images).toHaveLength(6);
  }, 30_000);
});

/* ================================================================== words */

describe('no string in the app states an old limit', () => {
  const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), '..');
  function sources(dir: string, out: string[] = []): string[] {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) sources(path, out);
      else if (/\.(ts|tsx)$/.test(name)) out.push(path);
    }
    return out;
  }

  it('no toast or label promises 5, 20, 512 MB or 4 GB', () => {
    // String literals only (quotes or backticks on the same line), so a
    // comment that tells the history ("was 5") is not a hit.
    const OLD = [
      /['"`][^'"`\n]*You can attach up to[^'"`\n]*['"`]/,
      /['"`][^'"`\n]*up to (5|five|20|twenty) (images|photos|documents|files|videos)[^'"`\n]*['"`]/i,
      /['"`][^'"`\n]*the limit is (10 MB|512 MB|4 GB)[^'"`\n]*['"`]/,
      /['"`][^'"`\n]*can carry at most (5|20|three|3) [^'"`\n]*['"`]/i,
      /['"`](512 MB|4 GB)['"`]/,
    ];
    const hits: string[] = [];
    for (const dir of ['app', 'components', 'lib']) {
      for (const file of sources(join(FRONTEND, dir))) {
        readFileSync(file, 'utf8')
          .split('\n')
          .forEach((line, i) => {
            if (/^\s*(\/\/|\*|\/\*)/.test(line)) return;
            if (OLD.some((re) => re.test(line))) hits.push(`${file.slice(FRONTEND.length + 1)}:${i + 1}: ${line.trim()}`);
          });
      }
    }
    expect(hits).toEqual([]);
  });
});
