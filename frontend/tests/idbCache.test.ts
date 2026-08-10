/**
 * IndexedDB persister (lib/idbCache) against fake-indexeddb: per-conversation
 * records, write-once image records rejoined on load, truncation cleanup,
 * and the broken→fallback path.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { IDBFactory, IDBKeyRange as IDBKeyRangePoly } from 'fake-indexeddb';
import {
  createIdbPersister,
  type CachePersister,
} from '../lib/idbCache';
import type { ChatMessage, Conversation } from '../lib/types';

function msg(
  content: string,
  extra: Partial<ChatMessage> = {},
): ChatMessage {
  return {
    id: `m-${Math.random().toString(36).slice(2)}`,
    role: 'user',
    content,
    createdAt: Date.now(),
    ...extra,
  };
}

function conv(id: string, messages: ChatMessage[]): Conversation {
  return { id, title: id, createdAt: 1, updatedAt: 1, messages };
}

function noopFallback(): CachePersister & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    mode: 'sync',
    loadAll: () => {
      calls.push('loadAll');
      return [];
    },
    put: () => void calls.push('put'),
    remove: () => void calls.push('remove'),
    clear: () => void calls.push('clear'),
  };
}

/** Read a store's raw records straight from the fake database. */
async function rawRecords(storeName: string): Promise<unknown[]> {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open('techsara-history', 1);
    open.onsuccess = () => {
      const db = open.result;
      const req = db
        .transaction(storeName, 'readonly')
        .objectStore(storeName)
        .getAll();
      req.onsuccess = () => {
        db.close();
        resolve(req.result as unknown[]);
      };
      req.onerror = () => reject(req.error);
    };
    open.onerror = () => reject(open.error);
  });
}

beforeEach(() => {
  (globalThis as { indexedDB: IDBFactory }).indexedDB = new IDBFactory();
  (globalThis as { IDBKeyRange: unknown }).IDBKeyRange = IDBKeyRangePoly;
});

describe('idb persister', () => {
  it('round-trips conversations and rejoins stripped images on load', async () => {
    const p = createIdbPersister(noopFallback());
    const withImages = conv('c1', [
      msg('look at these', {
        imageDataUrl: 'data:image/png;base64,AAA',
        imageDataUrls: ['data:image/png;base64,AAA', 'data:image/png;base64,BBB'],
      }),
      msg('plain'),
    ]);
    await p.put([withImages, conv('c2', [msg('no images')])]);

    // The conversation record itself must NOT carry base64 payloads …
    const stored = (await rawRecords('conversations')) as Conversation[];
    const c1 = stored.find((c) => c.id === 'c1');
    expect(c1?.messages[0]?.imageDataUrl).toBeUndefined();
    expect(c1?.messages[0]?.imageDataUrls).toBeUndefined();

    // … but loadAll puts them back exactly.
    const loaded = await p.loadAll();
    const restored = loaded.find((c) => c.id === 'c1');
    expect(restored?.messages[0]?.imageDataUrl).toBe('data:image/png;base64,AAA');
    expect(restored?.messages[0]?.imageDataUrls).toEqual([
      'data:image/png;base64,AAA',
      'data:image/png;base64,BBB',
    ]);
    expect(loaded.map((c) => c.id).sort()).toEqual(['c1', 'c2']);
  });

  it('treats image records as write-once (streaming re-puts never re-clone them)', async () => {
    const p = createIdbPersister(noopFallback());
    const first = msg('q', { imageDataUrl: 'data:image/png;base64,ORIGINAL' });
    await p.put([conv('c1', [first])]);

    // A later save of the same conversation mutates the in-memory copy;
    // the stored image record must keep the original.
    const mutated = { ...first, imageDataUrl: 'data:image/png;base64,CHANGED' };
    await p.put([conv('c1', [mutated, msg('answer')])]);

    const loaded = await p.loadAll();
    expect(loaded[0]?.messages[0]?.imageDataUrl).toBe(
      'data:image/png;base64,ORIGINAL',
    );
  });

  it('drops image records past the tail when a conversation is truncated', async () => {
    const p = createIdbPersister(noopFallback());
    const three = [
      msg('one'),
      msg('two'),
      msg('three', { imageDataUrl: 'data:image/png;base64,TAIL' }),
    ];
    await p.put([conv('c1', three)]);
    expect(await rawRecords('images')).toHaveLength(1);

    await p.put([conv('c1', three.slice(0, 2))]);
    expect(await rawRecords('images')).toHaveLength(0);
  });

  it('removes a conversation together with its image records', async () => {
    const p = createIdbPersister(noopFallback());
    await p.put([
      conv('gone', [msg('x', { imageDataUrl: 'data:image/png;base64,GONE' })]),
      conv('kept', [msg('y', { imageDataUrl: 'data:image/png;base64,KEPT' })]),
    ]);

    await p.remove(['gone']);

    const loaded = await p.loadAll();
    expect(loaded.map((c) => c.id)).toEqual(['kept']);
    const images = (await rawRecords('images')) as { convId: string }[];
    expect(images.map((i) => i.convId)).toEqual(['kept']);
  });

  it('clear() empties both stores', async () => {
    const p = createIdbPersister(noopFallback());
    await p.put([
      conv('c1', [msg('x', { imageDataUrl: 'data:image/png;base64,X' })]),
    ]);
    await p.clear();
    expect(await p.loadAll()).toEqual([]);
    expect(await rawRecords('images')).toHaveLength(0);
  });

  it('falls back and reports once when IndexedDB is unusable', async () => {
    delete (globalThis as { indexedDB?: unknown }).indexedDB;
    const fallback = noopFallback();
    let brokenCount = 0;
    const p = createIdbPersister(fallback, () => {
      brokenCount += 1;
    });

    expect(await p.loadAll()).toEqual([]);
    await p.put([conv('c1', [msg('x')])]);
    await p.clear();

    expect(brokenCount).toBe(1);
    expect(fallback.calls).toEqual(['loadAll', 'put', 'clear']);
  });
});
