/**
 * Chat media — the browser's half of storing every chat photo on our server
 * (2026-10-02, docs/chat-media/CONTRACT.md §7 and §10).
 *
 * The defect: a photo sent from a phone showed on the phone and nowhere else,
 * because its only lasting copy was `imageDataUrl` in that browser's
 * IndexedDB. These pin the pieces that make it show everywhere — what a turn
 * records (`meta.images`), where a stored photo is shown from, the box a
 * thumbnail reserves — and the BACKFILL that uploads the photos of older
 * turns from the browser that still holds them.
 *
 * Fixtures use low-entropy ids on purpose: the secret scanner flags anything
 * random-looking (memory note "gitleaks trips on fabricated UUID fixtures").
 */

import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  backfillAttachmentId,
  BACKFILL_CHATS_PER_TICK,
  chatMediaUrl,
  createBackfill,
  createMediaListCache,
  fetchChatMediaBlob,
  imagesGoByReference,
  imagesMetaFor,
  MAX_MEDIA_BYTES_PER_REQUEST,
  MAX_MEDIA_PER_REQUEST,
  legacyPhotoNoteId,
  listChatMedia,
  needsServerPhotoLookup,
  REPAIR_GRACE_MS,
  serverPhotoIntentOf,
  serverPhotoLookup,
  serverPhotosByIntent,
  showsLegacyPhotoNote,
  storedAttachmentIds,
  storedImagesOf,
  thumbBox,
  storeImagesForSend,
  uploadChatMedia,
  uploadChatMediaInBatches,
  withBackfilledImages,
  withImagesMeta,
  type BackfillHost,
  type ListedMediaItem,
  type MediaUploadOutcome,
} from '@/lib/chatMedia';
import { turnFingerprint } from '@/lib/idbCache';
import { INLINE_IMAGE_BUDGET_BYTES, MAX_IMAGES } from '@/lib/orchestrator';
import { MAX_ATTEMPTS } from '@/lib/uploadDocument';
import { MAX_MEDIA_BODY_BYTES } from '@/app/api/chat-media/_media';
import type { ChatMessage } from '@/lib/types';

afterEach(() => vi.unstubAllGlobals());

const CONV = 'conv-media-1';
const PNG = 'data:image/png;base64,iVBORw0KGgo=';
const JPEG = 'data:image/jpeg;base64,/9j/4AAQ';

function user(content: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return { id: `u-${content}`, role: 'user', content, createdAt: 1, ...extra };
}
function answer(content: string, route?: string): ChatMessage {
  return {
    id: `a-${content}`,
    role: 'assistant',
    content,
    createdAt: 2,
    ...(route ? { meta: { route: route as never } } : {}),
  };
}

/* ======================================================= what a turn says */

describe('meta.images — what a user turn records about its photos', () => {
  it('records id, name, the type the bytes were SENT as and the size, in order', () => {
    expect(
      imagesMetaFor([
        { attachment_id: 'img-aaaa-0001', name: 'cat.jpg', dataUrl: JPEG, width: 1600, height: 1200 },
        { attachment_id: 'img-aaaa-0002', name: 'shot.png', dataUrl: PNG },
      ]),
    ).toEqual([
      { attachment_id: 'img-aaaa-0001', name: 'cat.jpg', mime: 'image/jpeg', width: 1600, height: 1200 },
      { attachment_id: 'img-aaaa-0002', name: 'shot.png', mime: 'image/png' },
    ]);
  });

  it('never carries a data URL, a server id, progress or a timestamp', () => {
    const [entry] = imagesMetaFor([
      { attachment_id: 'img-aaaa-0001', name: 'cat.jpg', dataUrl: JPEG, width: 4, height: 3 },
    ])!;
    expect(Object.keys(entry).sort()).toEqual(['attachment_id', 'height', 'mime', 'name', 'width']);
    expect(JSON.stringify(entry)).not.toContain('data:');
  });

  it('skips a photo with no well-formed id, and says nothing when none is left', () => {
    expect(imagesMetaFor([{ attachment_id: 'short', dataUrl: PNG }])).toBeUndefined();
    expect(imagesMetaFor([{ attachment_id: '../../etc/passwd', dataUrl: PNG }])).toBeUndefined();
    expect(imagesMetaFor([])).toBeUndefined();
  });

  it('builds the same object every time — the sync key hashes it', () => {
    const one = { attachment_id: 'img-aaaa-0001', name: 'a.png', dataUrl: PNG, width: 10, height: 0 };
    // A half-known size is no size at all, so the key set never flickers.
    expect(imagesMetaFor([one])).toEqual(imagesMetaFor([{ ...one }]));
    expect(imagesMetaFor([one])![0]).not.toHaveProperty('width');
  });

  it('leaves a photo-less turn with exactly the meta it always had', () => {
    const meta = { route: 'chat' as const };
    expect(withImagesMeta(meta, undefined)).toBe(meta);
    expect(withImagesMeta(undefined, undefined)).toBeUndefined();
    expect(withImagesMeta(undefined, [{ attachment_id: 'img-aaaa-0001' }])).toEqual({
      images: [{ attachment_id: 'img-aaaa-0001' }],
    });
  });

  it('reads back only well-formed references', () => {
    expect(
      storedImagesOf({
        meta: { images: [{ attachment_id: 'img-aaaa-0001' }, { attachment_id: 'x' }, null] },
      }),
    ).toEqual([{ attachment_id: 'img-aaaa-0001' }]);
    expect(storedImagesOf({})).toEqual([]);
  });
});

describe('where a stored photo is shown from', () => {
  it('is a same-origin URL derived from the reference, encoded', () => {
    expect(chatMediaUrl({ conversationId: CONV, attachmentId: 'img-aaaa-0001' }, 'thumb')).toBe(
      '/api/chat-media/conv-media-1/img-aaaa-0001?size=thumb',
    );
    expect(chatMediaUrl({ conversationId: 'a b', attachmentId: 'c/d' }, 'full')).toBe(
      '/api/chat-media/a%20b/c%2Fd?size=full',
    );
  });

  it('reserves the box the local preview settles at, before a byte arrives', () => {
    // 1600×1200 shown at the bubble's 160 px height is 213 px wide.
    expect(thumbBox({ width: 1600, height: 1200 })).toEqual({
      width: '213px',
      maxWidth: '100%',
      height: 'auto',
      aspectRatio: '1600 / 1200',
    });
    // Smaller than the cap: its own size, as an <img> with max-h-40 would be.
    expect(thumbBox({ width: 80, height: 40 })?.width).toBe('80px');
    // No recorded size: the caller's fixed square.
    expect(thumbBox({})).toBeNull();
    expect(thumbBox({ width: 0, height: 10 })).toBeNull();
  });
});

describe('the legacy photo note', () => {
  it('shows for a turn with no stored photo, no local bytes and a vision answer', () => {
    expect(showsLegacyPhotoNote(user('is this healthy?'), answer('yes', 'vision'))).toBe(true);
  });

  it('stays silent whenever the photo can be shown, or the answer was not vision', () => {
    expect(
      showsLegacyPhotoNote(user('q', { imageDataUrl: PNG }), answer('a', 'vision')),
    ).toBe(false);
    expect(
      showsLegacyPhotoNote(
        user('q', { meta: { images: [{ attachment_id: 'img-aaaa-0001' }] } }),
        answer('a', 'vision'),
      ),
    ).toBe(false);
    expect(showsLegacyPhotoNote(user('q'), answer('a', 'document'))).toBe(false);
    expect(showsLegacyPhotoNote(user('q'), answer('a'))).toBe(false);
    expect(showsLegacyPhotoNote(user('q'), undefined)).toBe(false);
    expect(showsLegacyPhotoNote(answer('a', 'vision'), answer('b', 'vision'))).toBe(false);
  });

  // QA 2026-10-02: a text follow-up about a photo is answered by the vision
  // engine too (image memory), so "a vision answer under a photo-less turn"
  // used to put the line under every follow-up, on every device.
  it('stays silent on a text follow-up once the thread above holds a photo or a vision answer', () => {
    const followup = user('what colour is the stem?');
    const stemAnswer = answer('green', 'vision');
    const stored = user('is this leaf healthy?', {
      meta: { images: [{ attachment_id: 'img-aaaa-0001' }] },
    });
    expect(showsLegacyPhotoNote(followup, stemAnswer, [stored, answer('healthy', 'vision')])).toBe(
      false,
    );
    expect(
      showsLegacyPhotoNote(followup, stemAnswer, [user('q', { imageDataUrl: PNG }), answer('a')]),
    ).toBe(false);
    // A legacy photo turn above (no bytes here, but its vision answer).
    expect(showsLegacyPhotoNote(followup, stemAnswer, [user('q'), answer('a', 'vision')])).toBe(
      false,
    );
    // Nothing about a picture above: still the legacy line.
    expect(showsLegacyPhotoNote(followup, stemAnswer, [user('hi'), answer('hello')])).toBe(true);
  });

  it('legacyPhotoNoteId names at most one turn: the first photo-less turn under a vision answer', () => {
    const legacy = user('is this leaf healthy?');
    const thread = [
      user('hi'),
      answer('hello'),
      legacy,
      answer('healthy', 'vision'),
      user('and the stem?'),
      answer('green', 'vision'),
    ];
    expect(legacyPhotoNoteId(thread)).toBe(legacy.id);
    const withStoredPhoto = [
      user('leaf', { meta: { images: [{ attachment_id: 'img-aaaa-0001' }] } }),
      answer('healthy', 'vision'),
      user('and the stem?'),
      answer('green', 'vision'),
    ];
    expect(legacyPhotoNoteId(withStoredPhoto)).toBeNull();
    expect(legacyPhotoNoteId([user('hi'), answer('hello')])).toBeNull();
  });
});

/* ======================================================= reading the bytes */

describe('fetchChatMediaBlob', () => {
  it('reads 404 and 410 as "the server has no such photo"', async () => {
    for (const status of [404, 410]) {
      vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status })));
      expect(
        await fetchChatMediaBlob({ conversationId: CONV, attachmentId: 'img-aaaa-0001' }, 'full'),
      ).toEqual({ status: 'missing' });
    }
  });

  it('uses the HTTP cache (no cache: no-store) and returns the bytes', async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async () => new Response(new Blob(['png'], { type: 'image/png' }), { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const out = await fetchChatMediaBlob(
      { conversationId: CONV, attachmentId: 'img-aaaa-0001' },
      'full',
    );
    expect(out.status).toBe('ok');
    expect(fetchMock.mock.calls[0][0]).toBe('/api/chat-media/conv-media-1/img-aaaa-0001?size=full');
    expect(fetchMock.mock.calls[0][1]?.cache).toBeUndefined();
  });
});

/* ======================================================= uploading photos */

describe('uploadChatMedia', () => {
  it('posts file and attachment_id parts in the same order, plus the source', async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      Response.json({ items: [{ attachment_id: 'bf-one-aaaaaaaa' }, { attachment_id: 'bf-two-aaaaaaaa' }] }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const out = await uploadChatMedia(
      CONV,
      [
        { attachmentId: 'bf-one-aaaaaaaa', blob: new Blob(['1'], { type: 'image/png' }), name: 'image-1.png' },
        { attachmentId: 'bf-two-aaaaaaaa', blob: new Blob(['2'], { type: 'image/png' }), name: 'image-2.png' },
      ],
      'backfill',
    );
    expect(out.kind).toBe('stored');
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/chat-media/conv-media-1');
    expect(init?.method).toBe('POST');
    const form = init?.body as FormData;
    expect(form.getAll('attachment_id')).toEqual(['bf-one-aaaaaaaa', 'bf-two-aaaaaaaa']);
    expect((form.getAll('file') as File[]).map((f) => f.name)).toEqual(['image-1.png', 'image-2.png']);
    expect(form.get('source')).toBe('backfill');
  });

  it('tells each refusal apart', async () => {
    const cases: Array<[number, MediaUploadOutcome['kind']]> = [
      [400, 'refused'],
      [413, 'refused'],
      [415, 'refused'],
      [404, 'not_found'],
      [401, 'unauthenticated'],
      [507, 'no_space'],
      [502, 'failed'],
    ];
    for (const [status, kind] of cases) {
      vi.stubGlobal('fetch', vi.fn(async () => Response.json({}, { status })));
      expect((await uploadChatMedia(CONV, [], 'backfill')).kind).toBe(kind);
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    expect((await uploadChatMedia(CONV, [], 'backfill')).kind).toBe('offline');
  });
});

describe('uploads in batches under the budget (2026-10-03, LIMITS.md)', () => {
  /** A blob that claims `size` bytes without allocating them. */
  const sized = (size: number) => {
    const blob = new Blob(['x'], { type: 'image/png' });
    Object.defineProperty(blob, 'size', { value: size });
    return blob;
  };
  const part = (i: number, size: number) => ({
    attachmentId: `img-limit-${String(i).padStart(4, '0')}`,
    blob: sized(size),
    name: `image-${i}.png`,
  });
  /** Stores every POST; the list (GET) names `held`, or fails when null. */
  function stubStore(held: string[] | null = []) {
    const forms: FormData[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        if ((init?.method ?? 'GET') === 'GET') {
          return held
            ? Response.json({ items: held.map((attachment_id) => ({ attachment_id })) })
            : Response.json({}, { status: 503 });
        }
        const form = init?.body as FormData;
        forms.push(form);
        return Response.json({
          items: (form.getAll('attachment_id') as string[]).map((attachment_id) => ({ attachment_id })),
        });
      }),
    );
    return forms;
  }
  /** Yield (setImmediate is never faked here) until `ready`, or 2 s of real time. */
  async function until(ready: () => boolean): Promise<void> {
    const deadline = Date.now() + 2000;
    while (!ready() && Date.now() < deadline) {
      await new Promise((resolve) => setImmediate(resolve));
    }
  }
  /** Run a pending upload to its end under fake timers, a backoff at a time. */
  async function drain<T>(pending: Promise<T>): Promise<T> {
    let settled = false;
    void pending.then(
      () => (settled = true),
      () => (settled = true),
    );
    const deadline = Date.now() + 4000;
    while (!settled && Date.now() < deadline) {
      await vi.advanceTimersByTimeAsync(8_001);
      await new Promise((resolve) => setImmediate(resolve));
    }
    return pending;
  }

  it('a batch is cut by its bytes, and by the 999 ceiling only past that', async () => {
    // No count limit in the app: 999 is the technical ceiling, the server's
    // chat_media.MAX_FILES too. Bytes are what bound one request.
    expect(MAX_MEDIA_PER_REQUEST).toBe(MAX_IMAGES);
    expect(MAX_IMAGES).toBe(999);
    expect(MAX_MEDIA_BYTES_PER_REQUEST).toBe(INLINE_IMAGE_BUDGET_BYTES);
    const forms = stubStore();
    const small = Array.from({ length: 1001 }, (_, i) => part(i, 1024));
    const out = await uploadChatMediaInBatches(CONV, small, 'upload');
    expect(out.kind).toBe('stored');
    expect(forms.map((f) => f.getAll('file').length)).toEqual([999, 2]);
    expect(out.kind === 'stored' && out.items.map((i) => i.attachment_id)).toEqual(
      small.map((p) => p.attachmentId),
    );

    forms.length = 0;
    const tenMiB = 10 * 1024 * 1024;
    // 4 × 10 MiB = 40 MiB fits; the fifth would make 50 MiB > 48 MiB.
    await uploadChatMediaInBatches(CONV, Array.from({ length: 7 }, (_, i) => part(i, tenMiB)), 'upload');
    expect(forms.map((f) => f.getAll('file').length)).toEqual([4, 3]);
  });

  it('stops at the first request that is not stored and answers with it', async () => {
    let calls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        calls += 1;
        return calls === 1 ? Response.json({ items: [] }) : Response.json({}, { status: 507 });
      }),
    );
    const parts = Array.from({ length: 11 }, (_, i) => part(i, 10 * 1024 * 1024));
    expect((await uploadChatMediaInBatches(CONV, parts, 'upload')).kind).toBe('no_space');
    expect(calls).toBe(2);
  });

  it('a batch that fails for a passing reason goes again, alone, after a wait (QA 2026-10-03)', async () => {
    // 40 photos the browser could not shrink, 10 MiB each: ten requests of
    // four. Before, the 503 on the fifth failed the whole send, and the
    // person's retry sent all 400 MiB again.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      const tenMiB = 10 * 1024 * 1024;
      const parts = Array.from({ length: 40 }, (_, i) => part(i, tenMiB));
      const posted: string[][] = [];
      vi.stubGlobal(
        'fetch',
        vi.fn(async (_url: string, init?: RequestInit) => {
          const ids = (init?.body as FormData).getAll('attachment_id') as string[];
          posted.push(ids);
          if (posted.length === 5) return new Response('upstream', { status: 503 });
          if (posted.length === 8) throw new TypeError('Failed to fetch');
          return Response.json({ items: ids.map((attachment_id) => ({ attachment_id })) });
        }),
      );
      const pending = uploadChatMediaInBatches(CONV, parts, 'upload');
      await until(() => posted.length === 5 && vi.getTimerCount() === 1);
      // Not at once: the chunked rail's backoff first.
      expect(posted).toHaveLength(5);
      expect(vi.getTimerCount()).toBe(1);

      const out = await drain(pending);
      expect(out.kind).toBe('stored');
      expect(out.kind === 'stored' && out.items.map((i) => i.attachment_id)).toEqual(
        parts.map((p) => p.attachmentId),
      );
      // Ten batches and the two that failed, each sent again on its own:
      // 480 MiB in all, not 400 + 400.
      expect(posted).toHaveLength(12);
      expect(posted[5]).toEqual(posted[4]);
      expect(posted[8]).toEqual(posted[7]);
      expect(new Set(posted.flat()).size).toBe(40);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a passing failure is tried MAX_ATTEMPTS times; an answer is never asked again', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      let calls = 0;
      let status = 503;
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => {
          calls += 1;
          return Response.json({}, { status });
        }),
      );
      for (const passing of [429, 502, 503, 504]) {
        calls = 0;
        status = passing;
        const out = await drain(uploadChatMediaInBatches(CONV, [part(0, 1024)], 'upload'));
        expect(out).toEqual({ kind: 'failed', status: passing });
        expect(calls).toBe(MAX_ATTEMPTS);
      }
      // A refusal, a missing chat, a dead session, a server error and a full
      // disk say something about the request: one request, no wait.
      for (const answer of [400, 413, 415, 404, 401, 403, 500, 507]) {
        calls = 0;
        status = answer;
        await uploadChatMediaInBatches(CONV, [part(0, 1024)], 'upload');
        expect(calls).toBe(1);
        expect(vi.getTimerCount()).toBe(0);
      }
    } finally {
      vi.useRealTimers();
    }
  });

  it('Stop during the wait ends it at once, and nothing goes again', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      let calls = 0;
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => {
          calls += 1;
          return Response.json({}, { status: 503 });
        }),
      );
      const stop = new AbortController();
      const pending = uploadChatMediaInBatches(CONV, [part(0, 1024)], 'upload', stop.signal);
      await until(() => calls === 1 && vi.getTimerCount() === 1);
      expect(vi.getTimerCount()).toBe(1); // waiting to try again
      stop.abort();
      // Settles with no timer advanced: the backoff did not run out.
      expect((await pending).kind).toBe('failed');
      expect(calls).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('every batch fits the proxy (and the orchestrator) body cap with its framing', () => {
    expect(MAX_MEDIA_BYTES_PER_REQUEST + 1024 * 1024).toBeLessThanOrEqual(MAX_MEDIA_BODY_BYTES);
  });

  it('decides by the inline bytes, and only for photos with ids in a real chat', () => {
    const ids = ['img-limit-0001', 'img-limit-0002'];
    const over = INLINE_IMAGE_BUDGET_BYTES + 1;
    expect(imagesGoByReference(CONV, ['AAAA', 'BBBB'], ids, INLINE_IMAGE_BUDGET_BYTES)).toBe(false);
    expect(imagesGoByReference(CONV, ['AAAA', 'BBBB'], ids, over)).toBe(true);
    // No ids that pair with the bytes: nothing to name them by, so inline.
    expect(imagesGoByReference(CONV, ['AAAA', 'BBBB'], ids.slice(0, 1), over)).toBe(false);
    expect(imagesGoByReference(CONV, ['AAAA'], ['short'], over)).toBe(false);
    // A bare /chat session key is never a media conversation (F034).
    expect(imagesGoByReference('u7-abc', ['AAAA', 'BBBB'], ids, over)).toBe(false);
    expect(imagesGoByReference(CONV, [], [], over)).toBe(false);
  });

  it('stores a send\'s base64 under its ids, with the real type, the same photo once', async () => {
    const forms = stubStore();
    const png = 'iVBORw0KGgoAAAANSUhEUg==';
    const jpeg = '/9j/4AAQSkZJRgABAQ==';
    const out = await storeImagesForSend(
      CONV,
      [png, jpeg, png],
      ['img-limit-0001', 'img-limit-0002', 'img-limit-0001'],
    );
    expect(out.kind).toBe('stored');
    const files = forms[0].getAll('file') as File[];
    expect(forms[0].getAll('attachment_id')).toEqual(['img-limit-0001', 'img-limit-0002']);
    expect(files.map((f) => f.type)).toEqual(['image/png', 'image/jpeg']);
    expect(files.map((f) => f.name)).toEqual(['image-1.png', 'image-2.jpg']);
    expect(forms[0].get('source')).toBe('upload');
  });

  it('a retried send sends only the photos the server does not hold yet (QA 2026-10-03)', async () => {
    const png = 'iVBORw0KGgoAAAANSUhEUg==';
    const jpeg = '/9j/4AAQSkZJRgABAQ==';
    const images = [png, jpeg, png.replace('Ug', 'Ag'), jpeg.replace('AQ', 'AA')];
    const ids = ['img-limit-0001', 'img-limit-0002', 'img-limit-0003', 'img-limit-0004'];

    // An earlier attempt stored the first and third before it failed.
    const forms = stubStore(['img-limit-0001', 'img-limit-0003']);
    const out = await storeImagesForSend(CONV, images, ids);
    expect(out.kind).toBe('stored');
    expect(forms).toHaveLength(1);
    expect(forms[0].getAll('attachment_id')).toEqual(['img-limit-0002', 'img-limit-0004']);
    // A part keeps the name of its place in the message.
    expect((forms[0].getAll('file') as File[]).map((f) => f.name)).toEqual([
      'image-2.jpg',
      'image-4.jpg',
    ]);

    // All of them landed (the answer was lost): nothing is sent again.
    const none = stubStore(ids);
    expect((await storeImagesForSend(CONV, images, ids)).kind).toBe('stored');
    expect(none).toHaveLength(0);

    // The server could not say: every photo goes, as before (an id it
    // already holds comes back unchanged).
    const all = stubStore(null);
    expect((await storeImagesForSend(CONV, images, ids)).kind).toBe('stored');
    expect(all.flatMap((f) => f.getAll('attachment_id'))).toEqual(ids);
  });
});

/* ============================================================ the backfill */

describe('backfillAttachmentId', () => {
  it('is bf- plus the first 32 hex of sha256(dataUrl) — the same in every tab', async () => {
    const expected = createHash('sha256').update(PNG).digest('hex').slice(0, 32);
    expect(await backfillAttachmentId(PNG)).toBe(`bf-${expected}`);
    expect(await backfillAttachmentId(PNG)).toBe(await backfillAttachmentId(PNG));
    expect(await backfillAttachmentId(JPEG)).not.toBe(await backfillAttachmentId(PNG));
    expect(await backfillAttachmentId(PNG)).toMatch(/^bf-[0-9a-f]{32}$/);
  });
});

/** An in-memory host: a stored thread per conversation, photos by index. */
function fakeHost(
  threads: Record<string, ChatMessage[]>,
  photos: Record<string, Map<number, string[]>>,
  opts: { onSave?: (id: string, next: ChatMessage[]) => void; idle?: boolean } = {},
) {
  const saves: Array<{ id: string; messages: ChatMessage[] }> = [];
  const host: BackfillHost = {
    messages: (id) => threads[id] ?? null,
    localImages: async (id) => photos[id] ?? new Map(),
    save: async (id, next) => {
      saves.push({ id, messages: next });
      threads[id] = next;
      opts.onSave?.(id, next);
    },
    idle: () => opts.idle ?? true,
  };
  return { host, saves };
}

/** An upload that stores everything and echoes a size back. */
function storingUpload() {
  return vi.fn<typeof uploadChatMedia>(async (...[, parts]) => ({
    kind: 'stored',
    items: parts.map((p) => ({
      attachment_id: p.attachmentId,
      mime: 'image/png',
      width: 640,
      height: 480,
    })),
  }));
}

const now = (task: () => void) => task();

describe('the backfill', () => {
  it('uploads the photos of turns with no meta.images and writes them in ONE save', async () => {
    const threads = {
      [CONV]: [
        user('first photo'),
        answer('a1', 'vision'),
        user('two photos'),
        answer('a2', 'vision'),
        user('text only'),
        user('already stored', { meta: { images: [{ attachment_id: 'img-aaaa-0009' }] } }),
      ],
    };
    const photos = {
      [CONV]: new Map([
        [0, [PNG]],
        [2, [PNG, JPEG]],
        [5, [JPEG]],
      ]),
    };
    const { host, saves } = fakeHost(threads, photos);
    const upload = storingUpload();
    const list = vi.fn(async () => new Set(['img-aaaa-0009']));
    const backfill = createBackfill(host, {
      upload,
      list,
      schedule: now,
      locks: null,
      online: () => true,
    });

    expect(await backfill.runNow(CONV)).toBe('done');

    // One request per turn that needs it; never for the turn whose reference
    // the server holds.
    expect(upload).toHaveBeenCalledTimes(2);
    for (const call of upload.mock.calls) {
      expect(call[0]).toBe(CONV);
      expect(call[2]).toBe('backfill');
      for (const part of call[1]) expect(part.attachmentId).toMatch(/^bf-[0-9a-f]{32}$/);
    }
    // One save for the whole chat.
    expect(saves).toHaveLength(1);
    const saved = saves[0].messages;
    const pngId = await backfillAttachmentId(PNG);
    const jpegId = await backfillAttachmentId(JPEG);
    expect(saved[0].meta?.images).toEqual([
      { attachment_id: pngId, mime: 'image/png', width: 640, height: 480 },
    ]);
    expect(saved[2].meta?.images?.map((i) => i.attachment_id)).toEqual([pngId, jpegId]);
    expect(saved[4].meta).toBeUndefined();
    expect(saved[5].meta?.images).toEqual([{ attachment_id: 'img-aaaa-0009' }]);
  });

  it('sends the same picture twice in one turn as ONE part, referenced twice', async () => {
    const threads = { [CONV]: [user('twins')] };
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG, PNG]]]) });
    const upload = storingUpload();
    await createBackfill(host, { upload, schedule: now, locks: null }).runNow(CONV);
    expect(upload.mock.calls[0][1]).toHaveLength(1);
    expect(saves[0].messages[0].meta?.images).toHaveLength(2);
  });

  it('re-applies once when a 409 replaced its save with the server copy', async () => {
    const original = [user('photo'), answer('a', 'vision')];
    const threads: Record<string, ChatMessage[]> = { [CONV]: original };
    let refusals = 1;
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) }, {
      onSave: (id) => {
        // The push carrying the change was refused and the store adopted
        // the server's copy, which has no meta.images.
        if (refusals-- > 0) threads[id] = original.map((m) => ({ ...m }));
      },
    });
    await createBackfill(host, { upload: storingUpload(), schedule: now, locks: null }).runNow(CONV);
    expect(saves).toHaveLength(2);
    expect(threads[CONV][0].meta?.images).toHaveLength(1);
  });

  it('does not loop when the server copy keeps winning', async () => {
    const original = [user('photo')];
    const threads: Record<string, ChatMessage[]> = { [CONV]: original };
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) }, {
      onSave: (id) => {
        threads[id] = original.map((m) => ({ ...m }));
      },
    });
    await createBackfill(host, { upload: storingUpload(), schedule: now, locks: null }).runNow(CONV);
    expect(saves).toHaveLength(2);
  });

  it('stops for good on 507, and writes what landed before it', async () => {
    const threads = { [CONV]: [user('p1'), user('p2'), user('p3')] };
    const photos = { [CONV]: new Map([[0, [PNG]], [1, [JPEG]], [2, ['data:image/gif;base64,R0lG']]]) };
    const { host, saves } = fakeHost(threads, photos);
    let calls = 0;
    const upload = vi.fn<typeof uploadChatMedia>(async (...args) => {
      calls += 1;
      if (calls === 2) return { kind: 'no_space' };
      return storingUpload()(...args);
    });
    const backfill = createBackfill(host, { upload, schedule: now, locks: null });
    expect(await backfill.runNow(CONV)).toBe('halt');
    expect(upload).toHaveBeenCalledTimes(2);
    expect(saves[0].messages[0].meta?.images).toHaveLength(1);
    expect(saves[0].messages[1].meta).toBeUndefined();
  });

  it('ignores every request after a 507, for the rest of the page', async () => {
    const threads: Record<string, ChatMessage[]> = {
      'conv-a-aaaa': [user('p')],
      'conv-b-bbbb': [user('q')],
    };
    const photos = {
      'conv-a-aaaa': new Map([[0, [PNG]]]),
      'conv-b-bbbb': new Map([[0, [JPEG]]]),
    };
    const { host } = fakeHost(threads, photos);
    const upload = vi.fn(async () => ({ kind: 'no_space' }) as MediaUploadOutcome);
    const backfill = createBackfill(host, { upload, schedule: now, locks: null });
    backfill.request('conv-a-aaaa');
    await vi.waitFor(() => expect(backfill.halted).toBe(true));
    backfill.request('conv-b-bbbb');
    await new Promise((r) => setTimeout(r, 0));
    expect(upload).toHaveBeenCalledTimes(1);
  });

  it('does nothing while offline, and drops the queue when the network goes', async () => {
    const threads = { [CONV]: [user('p')] };
    const { host } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) });
    const upload = storingUpload();
    const backfill = createBackfill(host, { upload, schedule: now, locks: null, online: () => false });
    backfill.request(CONV);
    await new Promise((r) => setTimeout(r, 0));
    expect(upload).not.toHaveBeenCalled();

    const offlineUpload = vi.fn(async () => ({ kind: 'offline' }) as MediaUploadOutcome);
    const b2 = createBackfill(host, { upload: offlineUpload, schedule: now, locks: null });
    expect(await b2.runNow(CONV)).toBe('offline');
  });

  it('works through at most three chats per idle tick', async () => {
    const ids = ['conv-q-1', 'conv-q-2', 'conv-q-3', 'conv-q-4', 'conv-q-5'];
    const threads: Record<string, ChatMessage[]> = {};
    const photos: Record<string, Map<number, string[]>> = {};
    for (const id of ids) {
      threads[id] = [user(id)];
      photos[id] = new Map([[0, [PNG]]]);
    }
    const { host } = fakeHost(threads, photos);
    const upload = storingUpload();
    const ticks: Array<() => void> = [];
    const backfill = createBackfill(host, {
      upload,
      schedule: (task) => ticks.push(task),
      locks: null,
    });
    for (const id of ids) backfill.request(id);
    expect(ticks).toHaveLength(1); // one tick booked, however many requests

    ticks.shift()!();
    await vi.waitFor(() => expect(upload).toHaveBeenCalledTimes(BACKFILL_CHATS_PER_TICK));
    await vi.waitFor(() => expect(ticks).toHaveLength(1)); // the rest wait for the next
    expect(upload).toHaveBeenCalledTimes(3);
    ticks.shift()!();
    await vi.waitFor(() => expect(upload).toHaveBeenCalledTimes(5));
  });

  it('is single-flight per chat across tabs: a held lock means another tab has it', async () => {
    const threads = { [CONV]: [user('p')] };
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) });
    const upload = storingUpload();
    const names: string[] = [];
    const locks = {
      request: vi.fn(
        async (name: string, _opts: unknown, cb: (lock: unknown) => Promise<unknown>) => {
          names.push(name);
          return cb(null); // another tab holds it
        },
      ),
    } as unknown as Pick<LockManager, 'request'>;
    const backfill = createBackfill(host, { upload, schedule: now, locks });
    expect(await backfill.runNow(CONV)).toBe('busy');
    expect(names).toEqual([`techsara-chat-media-backfill:${CONV}`]);
    expect(upload).not.toHaveBeenCalled();
    expect(saves).toHaveLength(0);
  });

  it('runs under the lock when it is free', async () => {
    const threads = { [CONV]: [user('p')] };
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) });
    const locks = {
      request: async (_n: string, opts: { ifAvailable?: boolean }, cb: (l: unknown) => Promise<unknown>) => {
        expect(opts.ifAvailable).toBe(true);
        return cb({ name: 'held' });
      },
    } as unknown as Pick<LockManager, 'request'>;
    await createBackfill(host, { upload: storingUpload(), schedule: now, locks }).runNow(CONV);
    expect(saves).toHaveLength(1);
  });

  it('leaves a chat alone while a send owns it', async () => {
    const threads = { [CONV]: [user('p')] };
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) }, { idle: false });
    const upload = storingUpload();
    expect(await createBackfill(host, { upload, schedule: now, locks: null }).runNow(CONV)).toBe('busy');
    expect(upload).not.toHaveBeenCalled();
    expect(saves).toHaveLength(0);
  });

  it('does not ask again this session for a photo the server refused (415)', async () => {
    const threads = { [CONV]: [user('heic')] };
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) });
    const upload = vi.fn(async () => ({ kind: 'refused', status: 415 }) as MediaUploadOutcome);
    const backfill = createBackfill(host, { upload, schedule: now, locks: null });
    expect(await backfill.runNow(CONV)).toBe('done');
    expect(await backfill.runNow(CONV)).toBe('done');
    expect(upload).toHaveBeenCalledTimes(1);
    expect(saves).toHaveLength(0);
  });

  it('never runs for a reserved or malformed conversation id', async () => {
    const { host } = fakeHost({}, {});
    const schedule = vi.fn();
    const backfill = createBackfill(host, { upload: storingUpload(), schedule, locks: null });
    backfill.request('u12-session');
    backfill.request('../etc');
    backfill.request('');
    backfill.request(null);
    expect(schedule).not.toHaveBeenCalled();
  });

  it('takes no photo from a record written for ANOTHER turn that once sat at that index', async () => {
    // QA 2026-10-02 (security): records are keyed by index, and a thread
    // replaced under them left an old photo under an unrelated text turn,
    // which the backfill uploaded and wrote into that turn on every device.
    const threads = {
      [CONV]: [user('hello again'), answer('hi'), user('draft my resignation letter'), answer('Dear')],
    };
    const { host, saves } = fakeHost(threads, {});
    host.localImages = async () =>
      new Map([[2, { urls: [PNG], boundTo: turnFingerprint(user('what is this rash?')) }]]);
    const upload = storingUpload();
    await createBackfill(host, { upload, schedule: now, locks: null }).runNow(CONV);
    expect(upload).not.toHaveBeenCalled();
    expect(saves).toHaveLength(0);
  });

  it('uses a record that names its turn, and an older unnamed one only under a vision answer', async () => {
    const threads = {
      [CONV]: [user('leaf?'), answer('healthy', 'vision'), user('letter please'), answer('Dear')],
    };
    const { host, saves } = fakeHost(threads, {});
    host.localImages = async () =>
      new Map([
        [0, { urls: [PNG] }],
        [2, { urls: [JPEG] }],
      ]);
    const upload = storingUpload();
    await createBackfill(host, { upload, schedule: now, locks: null }).runNow(CONV);
    expect(upload).toHaveBeenCalledTimes(1);
    expect(saves[0].messages[0].meta?.images).toHaveLength(1);
    expect(saves[0].messages[2].meta).toBeUndefined();

    const bound = { [CONV]: [user('letter please'), answer('Dear')] };
    const second = fakeHost(bound, {});
    second.host.localImages = async () =>
      new Map([[0, { urls: [JPEG], boundTo: turnFingerprint(user('letter please')) }]]);
    const upload2 = storingUpload();
    await createBackfill(second.host, { upload: upload2, schedule: now, locks: null }).runNow(CONV);
    expect(upload2).toHaveBeenCalledTimes(1);
  });

  it('withBackfilledImages only writes onto the same turn, and keeps identity otherwise', () => {
    const msgs = [user('photo'), answer('a')];
    const found = new Map([[0, { content: 'photo', images: [{ attachment_id: 'bf-aaaaaaaa' }] }]]);
    const next = withBackfilledImages(msgs, found);
    expect(next).not.toBe(msgs);
    expect(next[1]).toBe(msgs[1]);
    // The words changed under it: not the same turn any more.
    const moved = withBackfilledImages([user('other words'), answer('a')], found);
    expect(moved[0].meta).toBeUndefined();
    // Already referenced: untouched, same array.
    const done = [user('photo', { meta: { images: [{ attachment_id: 'img-aaaa-0001' }] } })];
    expect(withBackfilledImages(done, found)).toBe(done);
  });
});

/* ======================================================= the repair */

describe('the repair: a referenced photo the server never stored', () => {
  // QA 2026-10-02: the browser writes meta.images at send and the server
  // stores the bytes later, from the /chat body. When that store is lost the
  // reference names nothing, and the backfill skipped every turn that had
  // one, although this browser still held the bytes.
  const sent = (extra: Partial<ChatMessage> = {}) =>
    user('what is this?', {
      imageDataUrl: PNG,
      meta: { images: [{ attachment_id: 'att-00000001', mime: 'image/png' }] },
      ...extra,
    });

  it('re-uploads the bytes under the SAME id, and writes nothing', async () => {
    const threads = { [CONV]: [sent(), answer('a cat', 'vision')] };
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) });
    const upload = storingUpload();
    const list = vi.fn(async () => new Set<string>());
    expect(
      await createBackfill(host, { upload, list, schedule: now, locks: null }).runNow(CONV),
    ).toBe('done');
    expect(list).toHaveBeenCalledTimes(1);
    expect(upload).toHaveBeenCalledTimes(1);
    const [conv, parts, source] = upload.mock.calls[0];
    expect(conv).toBe(CONV);
    expect(source).toBe('backfill');
    expect(parts.map((p) => p.attachmentId)).toEqual(['att-00000001']);
    expect(parts[0].blob.type).toBe('image/png');
    expect(saves).toHaveLength(0);
    expect(threads[CONV][0].meta?.images).toEqual([
      { attachment_id: 'att-00000001', mime: 'image/png' },
    ]);
  });

  it('asks once per chat and uploads only the ids the server lacks', async () => {
    const two = user('two', {
      imageDataUrls: [PNG, JPEG],
      meta: {
        images: [
          { attachment_id: 'att-00000002', mime: 'image/png' },
          { attachment_id: 'att-00000003', mime: 'image/jpeg' },
        ],
      },
    });
    const threads = { [CONV]: [sent(), answer('a'), two, answer('b')] };
    const photos = { [CONV]: new Map([[0, [PNG]], [2, [PNG, JPEG]]]) };
    const { host } = fakeHost(threads, photos);
    const upload = storingUpload();
    const list = vi.fn(async () => new Set(['att-00000001', 'att-00000002']));
    await createBackfill(host, { upload, list, schedule: now, locks: null }).runNow(CONV);
    expect(list).toHaveBeenCalledTimes(1);
    expect(upload).toHaveBeenCalledTimes(1);
    expect(upload.mock.calls[0][1].map((p) => p.attachmentId)).toEqual(['att-00000003']);
    expect(upload.mock.calls[0][1][0].blob.type).toBe('image/jpeg');
  });

  it('does nothing when the server holds them, or when it could not say', async () => {
    const threads = { [CONV]: [sent(), answer('a')] };
    const { host } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) });
    const upload = storingUpload();
    await createBackfill(host, {
      upload,
      list: async () => new Set(['att-00000001']),
      schedule: now,
      locks: null,
    }).runNow(CONV);
    await createBackfill(host, { upload, list: async () => null, schedule: now, locks: null }).runNow(
      CONV,
    );
    expect(upload).not.toHaveBeenCalled();
  });

  it('never repairs with bytes that are not provably that turn’s, or too soon after the send', async () => {
    const list = vi.fn(async () => new Set<string>());
    const cases: Array<{ turn: ChatMessage; held: unknown }> = [
      // Unnamed (older) record: could be another turn's photo.
      { turn: sent(), held: { urls: [PNG] } },
      // Named for another turn.
      { turn: sent(), held: { urls: [PNG], boundTo: turnFingerprint(user('other words')) } },
      // The recorded type disagrees with the bytes.
      { turn: sent(), held: [JPEG] },
      // Counts disagree.
      { turn: sent(), held: [PNG, PNG] },
      // The /chat store may still be landing.
      { turn: sent({ createdAt: 1_000_000 }), held: [PNG] },
    ];
    for (const { turn, held } of cases) {
      const { host } = fakeHost({ [CONV]: [turn, answer('a', 'vision')] }, {});
      host.localImages = async () => new Map([[0, held as string[]]]);
      const upload = storingUpload();
      await createBackfill(host, {
        upload,
        list,
        now: () => 1_000_000 + REPAIR_GRACE_MS - 1,
        schedule: now,
        locks: null,
      }).runNow(CONV);
      expect(upload).not.toHaveBeenCalled();
    }
  });

  it('stops on 507 like the backfill', async () => {
    const threads = { [CONV]: [sent(), answer('a')] };
    const { host } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) });
    const upload = vi.fn(async () => ({ kind: 'no_space' }) as MediaUploadOutcome);
    const backfill = createBackfill(host, {
      upload,
      list: async () => new Set<string>(),
      schedule: now,
      locks: null,
    });
    // 'halt' is what makes the queue stop for the page's life (tick).
    expect(await backfill.runNow(CONV)).toBe('halt');
  });
});

describe('storedAttachmentIds', () => {
  it('reads the ids of the list, and null for anything that is not a list', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({ items: [{ attachment_id: 'att-00000001' }, { attachment_id: '../x' }, null] }),
      ),
    );
    expect(await storedAttachmentIds(CONV)).toEqual(new Set(['att-00000001']));
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(`/api/chat-media/${CONV}`);
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({})));
    expect(await storedAttachmentIds(CONV)).toBeNull();
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 502 })));
    expect(await storedAttachmentIds(CONV)).toBeNull();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    expect(await storedAttachmentIds(CONV)).toBeNull();
  });
});

/* ============================================ STORE-ALWAYS: ix- photos */

describe('photos the server stored by itself (STORE-ALWAYS §2)', () => {
  // newIntentId's shape, low-entropy for the secret scanner.
  const INTENT = 'ab12'.repeat(8);
  const OTHER = 'cd34'.repeat(8);
  const ix = (i: number, intent = INTENT) => `ix-${intent}-${i}`;
  const oldPageTurn = (extra: Partial<ChatMessage> = {}) =>
    user('what is on this invoice?', {
      meta: { intent: { id: INTENT, state: 'completed' } },
      ...extra,
    });
  const listed = (...items: ListedMediaItem[]) => vi.fn(async () => items);

  it('a turn is looked up only without references, without local bytes, with a 32-hex intent', () => {
    expect(serverPhotoIntentOf(oldPageTurn())).toBe(INTENT);
    expect(needsServerPhotoLookup(oldPageTurn())).toBe(true);
    // Its own references win; local bytes show at once; no lookup either way.
    expect(
      needsServerPhotoLookup(oldPageTurn({ meta: { intent: { id: INTENT, state: 'completed' }, images: [{ attachment_id: 'img-aaaa-0001' }] } })),
    ).toBe(false);
    expect(needsServerPhotoLookup(oldPageTurn({ imageDataUrl: PNG }))).toBe(false);
    // The base36 fallback intent is never minted from, nor an upper-case one.
    expect(serverPhotoIntentOf(user('q', { meta: { intent: { id: 'kx9abc12de34fg', state: 'completed' } } }))).toBeNull();
    expect(serverPhotoIntentOf(user('q', { meta: { intent: { id: INTENT.toUpperCase(), state: 'completed' } } }))).toBeNull();
    expect(serverPhotoIntentOf(user('q'))).toBeNull();
    expect(serverPhotoIntentOf({ ...answer('a'), meta: { intent: { id: INTENT, state: 'completed' } } } as ChatMessage)).toBeNull();
  });

  it('groups a list by intent, in SEND order (index), with the listed type and size', () => {
    const byIntent = serverPhotosByIntent([
      { attachment_id: ix(2), mime: 'image/png', width: 10, height: 20 },
      { attachment_id: ix(0), mime: 'image/jpeg' },
      { attachment_id: 'bf-' + '0'.repeat(32) },
      { attachment_id: ix(0, OTHER), mime: 'image/webp', width: 5, height: 5 },
      { attachment_id: ix(1), width: 0, height: 9 },
      { attachment_id: `ix-${INTENT}-x` },
    ]);
    expect(byIntent.get(INTENT)).toEqual([
      { attachment_id: ix(0), mime: 'image/jpeg' },
      { attachment_id: ix(1) },
      { attachment_id: ix(2), mime: 'image/png', width: 10, height: 20 },
    ]);
    expect(byIntent.get(OTHER)).toEqual([
      { attachment_id: ix(0, OTHER), mime: 'image/webp', width: 5, height: 5 },
    ]);
    expect(byIntent.size).toBe(2);
  });

  it('a turn of many photos (no limit, 999 ceiling) keeps every ix- photo in numeric send order', () => {
    // The server mints ix-<intent>-0..N-1 for N up to MAX_IMAGES, so indexes
    // reach three digits: 10 sorts after 9, never after 1, and 998 is the last.
    const indexes = [...Array(120).keys(), MAX_IMAGES - 1];
    const shuffled = [...indexes].sort((a, b) => String(a).localeCompare(String(b)));
    const byIntent = serverPhotosByIntent(shuffled.map((i) => ({ attachment_id: ix(i) })));
    expect(byIntent.get(INTENT)?.map((image) => image.attachment_id)).toEqual(indexes.map((i) => ix(i)));
    expect(ix(MAX_IMAGES - 1)).toHaveLength(39);
    // Past the ceiling is never a server-minted id.
    expect(serverPhotosByIntent([{ attachment_id: ix(MAX_IMAGES + 1) }]).size).toBe(0);
  });

  it('the lookup tells found, none and not-known-yet apart', () => {
    const known = serverPhotoLookup(serverPhotosByIntent([{ attachment_id: ix(0) }]));
    expect(known(oldPageTurn())).toEqual([{ attachment_id: ix(0) }]);
    expect(known(user('q', { meta: { intent: { id: OTHER, state: 'completed' } } }))).toEqual([]);
    expect(known(user('no intent'))).toEqual([]);
    const pending = serverPhotoLookup(undefined);
    expect(pending(oldPageTurn())).toBeUndefined();
    // A turn the list can say nothing about is "none" even while pending.
    expect(pending(user('no intent'))).toEqual([]);
  });

  it('the legacy line waits for the list, and is said only when it holds nothing', () => {
    const thread = [oldPageTurn(), answer('a total of 42', 'vision')];
    const found = serverPhotoLookup(serverPhotosByIntent([{ attachment_id: ix(0) }]));
    const none = serverPhotoLookup(serverPhotosByIntent([{ attachment_id: ix(0, OTHER) }]));
    expect(legacyPhotoNoteId(thread, found)).toBeNull();
    expect(legacyPhotoNoteId(thread, none)).toBe(thread[0].id);
    expect(legacyPhotoNoteId(thread, serverPhotoLookup(undefined))).toBeNull();
    // Without a lookup, exactly as before.
    expect(legacyPhotoNoteId(thread)).toBe(thread[0].id);
    // A follow-up under a found photo is a follow-up, not a legacy turn.
    const followUp = [
      ...thread,
      user('and the due date?', { meta: { intent: { id: OTHER, state: 'completed' } } }),
      answer('Friday', 'vision'),
    ];
    expect(legacyPhotoNoteId(followUp, found)).toBeNull();
  });

  it('the list is read once per chat however often it is asked, and again after a failure or forget', async () => {
    let fail = true;
    const list = vi.fn(async () => (fail ? null : [{ attachment_id: ix(0) }]));
    const cache = createMediaListCache(list);
    const [a, b] = await Promise.all([cache.get(CONV), cache.get(CONV)]);
    expect(a).toBeNull();
    expect(b).toBeNull();
    expect(list).toHaveBeenCalledTimes(1);
    fail = false;
    expect(await cache.get(CONV)).toEqual([{ attachment_id: ix(0) }]);
    expect(await cache.get(CONV)).toEqual([{ attachment_id: ix(0) }]);
    expect(list).toHaveBeenCalledTimes(2);
    cache.forget(CONV);
    await cache.get(CONV);
    expect(list).toHaveBeenCalledTimes(3);
    await cache.get('another-chat');
    expect(list).toHaveBeenCalledTimes(4);
  });

  it('listChatMedia keeps id, type and a real size, and nothing malformed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({
          items: [
            { attachment_id: ix(0), mime: 'image/png', width: 640, height: 480, bytes: 9, media_id: 'm' },
            { attachment_id: ix(1), width: null, height: -1 },
            { attachment_id: '../x' },
            null,
          ],
        }),
      ),
    );
    expect(await listChatMedia(CONV)).toEqual([
      { attachment_id: ix(0), mime: 'image/png', width: 640, height: 480 },
      { attachment_id: ix(1) },
    ]);
  });

  it('the sending browser adopts ix- rows instead of uploading a bf- duplicate', async () => {
    const threads = { [CONV]: [oldPageTurn({ imageDataUrl: PNG }), answer('42', 'vision')] };
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) });
    const upload = storingUpload();
    const media = listed({ attachment_id: ix(0), mime: 'image/png', width: 640, height: 480 });
    expect(
      await createBackfill(host, { upload, media, schedule: now, locks: null }).runNow(CONV),
    ).toBe('done');
    expect(upload).not.toHaveBeenCalled();
    expect(media).toHaveBeenCalledTimes(1);
    expect(saves).toHaveLength(1);
    expect(saves[0].messages[0].meta?.images).toEqual([
      { attachment_id: ix(0), mime: 'image/png', width: 640, height: 480 },
    ]);
    // The intent is untouched.
    expect(saves[0].messages[0].meta?.intent?.id).toBe(INTENT);
  });

  it('a device without the bytes writes the references too — one save for the chat', async () => {
    const threads = {
      [CONV]: [
        oldPageTurn(),
        answer('42', 'vision'),
        user('second', { meta: { intent: { id: OTHER, state: 'completed' } } }),
        answer('ok', 'vision'),
      ],
    };
    const { host, saves } = fakeHost(threads, {});
    const media = listed({ attachment_id: ix(1) }, { attachment_id: ix(0) }, { attachment_id: ix(0, OTHER) });
    await createBackfill(host, { upload: storingUpload(), media, schedule: now, locks: null }).runNow(CONV);
    expect(saves).toHaveLength(1);
    expect(saves[0].messages[0].meta?.images?.map((i) => i.attachment_id)).toEqual([ix(0), ix(1)]);
    expect(saves[0].messages[2].meta?.images?.map((i) => i.attachment_id)).toEqual([ix(0, OTHER)]);
  });

  it('re-applies the adoption once after a 409, like the backfill', async () => {
    const original = [oldPageTurn(), answer('42', 'vision')];
    const threads: Record<string, ChatMessage[]> = { [CONV]: original };
    let refusals = 1;
    const { host, saves } = fakeHost(threads, {}, {
      onSave: (id) => {
        if (refusals-- > 0) threads[id] = original.map((m) => ({ ...m }));
      },
    });
    await createBackfill(host, {
      upload: storingUpload(),
      media: listed({ attachment_id: ix(0) }),
      schedule: now,
      locks: null,
    }).runNow(CONV);
    expect(saves).toHaveLength(2);
    expect(threads[CONV][0].meta?.images).toEqual([{ attachment_id: ix(0) }]);
  });

  it('uploads bf- copies as before when the list holds nothing for the turn', async () => {
    const threads = { [CONV]: [oldPageTurn({ imageDataUrl: PNG }), answer('42', 'vision')] };
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) });
    const upload = storingUpload();
    await createBackfill(host, {
      upload,
      media: listed({ attachment_id: ix(0, OTHER) }),
      schedule: now,
      locks: null,
    }).runNow(CONV);
    expect(upload).toHaveBeenCalledTimes(1);
    expect(saves[0].messages[0].meta?.images?.[0].attachment_id).toBe(await backfillAttachmentId(PNG));
  });

  it('an unreadable list holds back only the turns the server may have stored itself', async () => {
    const threads = {
      [CONV]: [
        oldPageTurn({ imageDataUrl: PNG }),
        answer('42', 'vision'),
        user('older photo, no intent', { imageDataUrl: JPEG }),
        answer('ok', 'vision'),
      ],
    };
    const { host, saves } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]], [2, [JPEG]]]) });
    const upload = storingUpload();
    await createBackfill(host, {
      upload,
      media: vi.fn(async () => null),
      schedule: now,
      locks: null,
    }).runNow(CONV);
    expect(upload).toHaveBeenCalledTimes(1);
    expect(upload.mock.calls[0][1][0].blob.type).toBe('image/jpeg');
    expect(saves[0].messages[0].meta?.images).toBeUndefined();
    expect(saves[0].messages[2].meta?.images).toHaveLength(1);
  });

  it('never reads the list for a chat whose turns carry no usable intent', async () => {
    const threads = { [CONV]: [user('photo'), answer('a', 'vision')] };
    const { host } = fakeHost(threads, { [CONV]: new Map([[0, [PNG]]]) });
    const media = vi.fn(async () => [] as ListedMediaItem[]);
    await createBackfill(host, { upload: storingUpload(), media, schedule: now, locks: null }).runNow(CONV);
    expect(media).not.toHaveBeenCalled();
  });
});
