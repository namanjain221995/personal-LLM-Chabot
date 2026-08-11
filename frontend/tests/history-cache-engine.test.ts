/**
 * V4 cache engine: the server store's in-memory cache persisted write-behind
 * through a CachePersister (IndexedDB in the browser, legacy localStorage
 * blob as fallback). These tests drive the engine through the public
 * ServerHistoryStore interface with a recording fake persister.
 */
import { describe, expect, it } from 'vitest';
import {
  createHistoryStore,
  createServerHistoryStore,
  type StorageLike,
} from '../lib/history';
import type { CachePersister } from '../lib/idbCache';
import type { HistoryApi } from '../lib/historyApi';
import type { ChatMessage, Conversation } from '../lib/types';

function makeStorage(maxBytes = Infinity): StorageLike {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => {
      if (v.length > maxBytes) {
        const err = new Error('quota exceeded');
        err.name = 'QuotaExceededError';
        throw err;
      }
      map.set(k, v);
    },
    removeItem: (k) => void map.delete(k),
  };
}

/** HistoryApi where every call quietly succeeds (server sync is not under test). */
function quietApi(): HistoryApi {
  return {
    list: async () => [],
    get: async () => ({ id: '', title: '', messages: [] }),
    create: async () => undefined,
    update: async () => undefined,
    remove: async () => undefined,
    appendMessage: async () => undefined,
  setFeedback: async () => undefined,
  generateTitle: async () => ({ title: '', generated: false }),
    replaceMessages: async () => undefined,
    truncateMessages: async () => undefined,
  };
}

interface FakePersister extends CachePersister {
  puts: Conversation[][];
  removes: string[][];
  cleared: number;
}

function makePersister(initial: Conversation[] = []): FakePersister {
  const p: FakePersister = {
    mode: 'async',
    puts: [],
    removes: [],
    cleared: 0,
    loadAll: async () => initial,
    put(convs) {
      p.puts.push(convs.map((c) => ({ ...c })));
    },
    remove(ids) {
      p.removes.push([...ids]);
    },
    clear() {
      p.cleared += 1;
    },
  };
  return p;
}

function msg(role: 'user' | 'assistant', content: string): ChatMessage {
  return {
    id: `${role}-${Math.random().toString(36).slice(2)}`,
    role,
    content,
    createdAt: Date.now(),
  };
}

describe('server store cache engine (async persister)', () => {
  it('persists only the conversations a write touched', async () => {
    const persister = makePersister();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: quietApi(),
      persister,
    });
    await store.ready();
    const a = store.create('first');
    const b = store.create('second');
    await store.flush();
    persister.puts.length = 0;

    store.saveMessages(a.id, [msg('user', 'first')]);
    await store.flush();

    const persistedIds = persister.puts.flat().map((c) => c.id);
    expect(persistedIds).toContain(a.id);
    expect(persistedIds).not.toContain(b.id);
  });

  it('coalesces streaming writes into one batch per flush', async () => {
    const persister = makePersister();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: quietApi(),
      persister,
    });
    await store.ready();
    const conv = store.create('stream');
    await store.flush();
    persister.puts.length = 0;

    // Simulate token streaming: many saves before the debounce window ends.
    const thread = [msg('user', 'q')];
    for (let i = 0; i < 20; i++) {
      thread.length = 1;
      thread.push({ ...msg('assistant', 'token'.repeat(i + 1)) });
      store.saveMessages(conv.id, [...thread]);
    }
    await store.flush();

    expect(persister.puts.length).toBe(1);
    expect(persister.puts[0].map((c) => c.id)).toEqual([conv.id]);
  });

  it('propagates removals to the persister', async () => {
    const persister = makePersister();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: quietApi(),
      persister,
    });
    await store.ready();
    const conv = store.create('to delete');
    await store.flush();

    store.remove(conv.id);
    await store.flush();

    expect(persister.removes.flat()).toContain(conv.id);
  });

  it('merges hydration under conversations created during it — memory wins', async () => {
    let resolveLoad: (convs: Conversation[]) => void = () => undefined;
    const persister = makePersister();
    persister.loadAll = () =>
      new Promise<Conversation[]>((resolve) => {
        resolveLoad = resolve;
      });
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: quietApi(),
      persister,
    });

    const fresh = store.create('typed before hydration finished');
    resolveLoad([
      {
        id: 'old-1',
        title: 'from IndexedDB',
        createdAt: 1,
        updatedAt: 1,
        messages: [msg('user', 'hello')],
      },
    ]);
    await store.ready();

    const ids = store.list().map((c) => c.id);
    expect(ids).toContain(fresh.id);
    expect(ids).toContain('old-1');
  });

  it('imports the legacy localStorage blob once, then deletes it', async () => {
    const storage = makeStorage();
    // Write an authentic v1 blob through the v1 store.
    const v1 = createHistoryStore(storage);
    const legacyConv = v1.create('legacy conversation');
    v1.saveMessages(legacyConv.id, [msg('user', 'old data')]);
    expect(storage.getItem('techsara.history.v1')).not.toBeNull();

    const persister = makePersister([]);
    const store = createServerHistoryStore({
      storage,
      api: quietApi(),
      persister,
    });
    await store.ready();

    expect(store.list().map((c) => c.id)).toContain(legacyConv.id);
    expect(store.get(legacyConv.id)?.messages[0]?.content).toBe('old data');
    expect(storage.getItem('techsara.history.v1')).toBeNull();
    expect(persister.puts.flat().map((c) => c.id)).toContain(legacyConv.id);
  });

  it('clears the persister when a different user signs in', async () => {
    const persister = makePersister();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: quietApi(),
      persister,
    });
    await store.ready();
    store.setActiveUser('alice');
    store.create('alices chat');
    await store.flush();

    store.setActiveUser('bob');
    expect(store.list()).toHaveLength(0);
    expect(persister.cleared).toBeGreaterThanOrEqual(1);
  });

  it('keeps working memory-only when the persister cannot hydrate', async () => {
    const persister = makePersister();
    persister.loadAll = async () => {
      throw new Error('storage unavailable');
    };
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: quietApi(),
      persister,
    });
    await store.ready();
    const conv = store.create('still works');
    expect(store.list().map((c) => c.id)).toContain(conv.id);
  });
});

describe('server store cache engine (blob fallback, no IndexedDB)', () => {
  it('defaults to the legacy blob with its quota-evict behavior intact', async () => {
    const evicted: string[] = [];
    const storage = makeStorage(4_000);
    const store = createServerHistoryStore({
      storage,
      api: quietApi(),
      onEvict: (d) => evicted.push(d.id),
    });
    await store.ready(); // immediate for the sync engine
    const oldest = store.create('oldest');
    store.saveMessages(oldest.id, [msg('user', 'y'.repeat(2_000))]);
    const busy = store.create('busy');
    store.saveMessages(busy.id, [msg('assistant', 'x'.repeat(3_000))]);
    await store.flush();

    expect(evicted).toContain(oldest.id);
    // Memory keeps the session copy; only the on-disk mirror was trimmed.
    expect(store.list().map((c) => c.id)).toContain(oldest.id);
  });
});
