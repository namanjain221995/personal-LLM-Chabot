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
  fetchChatMediaBlob,
  imagesMetaFor,
  showsLegacyPhotoNote,
  storedImagesOf,
  thumbBox,
  uploadChatMedia,
  withBackfilledImages,
  withImagesMeta,
  type BackfillHost,
  type MediaUploadOutcome,
  type MediaUploadPart,
} from '@/lib/chatMedia';
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
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit) =>
        new Response(new Blob(['png'], { type: 'image/png' }), { status: 200 }),
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
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) =>
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
  return vi.fn(async (_conv: string, parts: MediaUploadPart[]): Promise<MediaUploadOutcome> => ({
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
    const backfill = createBackfill(host, { upload, schedule: now, locks: null, online: () => true });

    expect(await backfill.runNow(CONV)).toBe('done');

    // One request per turn that needs it; never for the turn that has refs.
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
    const upload = vi.fn(async (conv: string, parts: MediaUploadPart[]) => {
      calls += 1;
      if (calls === 2) return { kind: 'no_space' } as MediaUploadOutcome;
      return storingUpload()(conv, parts);
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
