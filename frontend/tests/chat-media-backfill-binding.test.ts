/**
 * Chat media backfill: a photo record belongs to ONE turn (2026-10-02,
 * security QA on docs/chat-media/CONTRACT.md §10).
 *
 * IndexedDB keeps photos in write-once records keyed `<conversation>#<index>`
 * and drops one only past the thread's end. When a thread is replaced under
 * them (a 409 adopting another device's copy, an old truncate-then-regrow),
 * a record survives under whatever turn now sits at its index. The backfill
 * read photos by index, so it uploaded that old photo and wrote it into an
 * unrelated text turn's `meta.images` — on every device, for good (first
 * write wins), and into the model's follow-up context.
 *
 * Now a record names the turn it was written for (role + exact words), the
 * boot read lays it only over that turn, and the backfill takes it only for
 * that turn. A record written before that field existed is used only under a
 * vision answer — the one hint such a turn ever carried.
 *
 * Runs the REAL persister on fake-indexeddb, the REAL history store and the
 * REAL backfill; only the upload is a double.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { IDBFactory, IDBKeyRange as IDBKeyRangePoly } from 'fake-indexeddb';
import {
  createIdbPersister,
  turnFingerprint,
  type CachePersister,
  type HeldImageRecord,
} from '@/lib/idbCache';
import { createBackfill, type BackfillHost, type uploadChatMedia } from '@/lib/chatMedia';
import { createServerHistoryStore, type StorageLike } from '@/lib/history';
import type { HistoryApi } from '@/lib/historyApi';
import type { ChatMessage, Conversation } from '@/lib/types';

const CONV = 'conv-binding-1';
const OLD_PHOTO = 'data:image/png;base64,iVBORw0KGgo=';

function m(
  role: 'user' | 'assistant',
  content: string,
  extra: Partial<ChatMessage> = {},
): ChatMessage {
  return { id: `${role}-${content}`, role, content, createdAt: 1, ...extra };
}
function conv(messages: ChatMessage[]): Conversation {
  return { id: CONV, title: CONV, createdAt: 1, updatedAt: 1, messages };
}
const fallback: CachePersister = {
  mode: 'sync',
  loadAll: () => [],
  put: () => undefined,
  remove: () => undefined,
  clear: () => undefined,
};

function storingUpload() {
  return vi.fn<typeof uploadChatMedia>(async (...[, parts]) => ({
    kind: 'stored',
    items: parts.map((p) => ({ attachment_id: p.attachmentId, mime: 'image/png', width: 1, height: 1 })),
  }));
}

function hostOver(
  thread: ChatMessage[],
  localImages: BackfillHost['localImages'],
): BackfillHost & { saved: ChatMessage[][] } {
  const saved: ChatMessage[][] = [];
  return {
    saved,
    messages: () => thread,
    localImages,
    save: async (_id, next) => {
      saved.push(next);
    },
    idle: () => true,
  };
}

const now = (task: () => void) => task();

beforeEach(() => {
  (globalThis as { indexedDB: IDBFactory }).indexedDB = new IDBFactory();
  (globalThis as { IDBKeyRange: unknown }).IDBKeyRange = IDBKeyRangePoly;
});

describe('a photo record written for one turn', () => {
  it('is never uploaded for, or shown on, the different turn a replaced thread put at its index', async () => {
    const persister = createIdbPersister(fallback);
    // This browser sent a photo at index 2.
    await persister.put([
      conv([
        m('user', 'hello'),
        m('assistant', 'hi'),
        m('user', 'what is this rash?', { imageDataUrl: OLD_PHOTO }),
        m('assistant', 'it looks like ...'),
      ]),
    ]);
    // The thread was replaced by another device's copy: index 2 is now an
    // unrelated text turn. The record survives (write-once, still in range).
    const serverThread = [
      m('user', 'hello again'),
      m('assistant', 'hi again'),
      m('user', 'draft my resignation letter'),
      m('assistant', 'Dear ...'),
    ];
    await persister.put([conv(serverThread)]);

    const records = await persister.loadImages!(CONV);
    expect(records.get(2)?.urls).toEqual([OLD_PHOTO]);
    expect(records.get(2)?.boundTo).toBe(turnFingerprint(m('user', 'what is this rash?')));

    // The boot read does not lay it over the new turn ...
    const [loaded] = (await persister.loadAll()) as Conversation[];
    expect(loaded.messages[2].imageDataUrl).toBeUndefined();

    // ... and the backfill neither uploads it nor references it.
    const upload = storingUpload();
    const host = hostOver(serverThread, (id) => persister.loadImages!(id));
    await createBackfill(host, { upload, schedule: now, locks: null }).runNow(CONV);
    expect(upload).not.toHaveBeenCalled();
    expect(host.saved).toHaveLength(0);
  });

  it('still belongs to its own turn after a hydrate wrote the server copy over the cache', async () => {
    const persister = createIdbPersister(fallback);
    const asked = m('user', 'is this leaf healthy?', { imageDataUrl: OLD_PHOTO });
    await persister.put([conv([asked, m('assistant', 'yes', { meta: { route: 'vision' } })])]);
    // The server's copy carries no browser-only fields.
    const serverThread = [
      m('user', 'is this leaf healthy?'),
      m('assistant', 'yes', { meta: { route: 'vision' } }),
    ];
    await persister.put([conv(serverThread)]);

    const [loaded] = (await persister.loadAll()) as Conversation[];
    expect(loaded.messages[0].imageDataUrl).toBe(OLD_PHOTO);

    const upload = storingUpload();
    const host = hostOver(serverThread, (id) => persister.loadImages!(id));
    await createBackfill(host, { upload, schedule: now, locks: null }).runNow(CONV);
    expect(upload).toHaveBeenCalledTimes(1);
    expect(host.saved[0][0].meta?.images?.[0]?.attachment_id).toMatch(/^bf-[0-9a-f]{32}$/);
  });
});

describe('a record written before records named their turn', () => {
  /** A history store over a persister whose only record names no turn. */
  function storeWithLegacyRecord(thread: ChatMessage[], index: number) {
    const map = new Map<string, string>();
    const storage: StorageLike = {
      getItem: (k) => map.get(k) ?? null,
      setItem: (k, v) => void map.set(k, v),
      removeItem: (k) => void map.delete(k),
    };
    // What the boot read hands over: the record laid over the turn at its
    // index (the in-memory copy), and the record itself.
    const booted = thread.map((msg, i) => (i === index ? { ...msg, imageDataUrl: OLD_PHOTO } : msg));
    const persister: CachePersister = {
      mode: 'sync',
      loadAll: () => [conv(booted)],
      put: () => undefined,
      remove: () => undefined,
      clear: () => undefined,
      loadImages: async () => new Map<number, HeldImageRecord>([[index, { urls: [OLD_PHOTO] }]]),
    };
    return createServerHistoryStore({ storage, api: {} as HistoryApi, persister });
  }

  it('is not vouched for by the copy the boot read laid over the turn', async () => {
    const thread = [m('user', 'draft my resignation letter'), m('assistant', 'Dear ...')];
    const store = storeWithLegacyRecord(thread, 0);
    const held = await store.localImages!(CONV);
    expect(held.get(0)).toEqual({ urls: [OLD_PHOTO] });

    const upload = storingUpload();
    const host = hostOver(thread, (id) => store.localImages!(id));
    await createBackfill(host, { upload, schedule: now, locks: null }).runNow(CONV);
    expect(upload).not.toHaveBeenCalled();
  });

  it('is backfilled under a vision answer, the hint every such photo turn carries', async () => {
    const thread = [
      m('user', 'is this leaf healthy?'),
      m('assistant', 'yes', { meta: { route: 'vision' } }),
    ];
    const store = storeWithLegacyRecord(thread, 0);
    const upload = storingUpload();
    const host = hostOver(thread, (id) => store.localImages!(id));
    await createBackfill(host, { upload, schedule: now, locks: null }).runNow(CONV);
    expect(upload).toHaveBeenCalledTimes(1);
  });
});
