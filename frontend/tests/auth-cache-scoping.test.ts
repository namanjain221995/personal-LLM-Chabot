/**
 * Account-scoped local data (enterprise auth retrofit).
 *
 * The history cache, sync bookkeeping, composer prefs and thumbs all live
 * in origin-wide storage — on a shared browser they are one account's data
 * only because the store makes them so. Pinned here: binding a DIFFERENT
 * account wipes every one of those (not just the conversation cache, which
 * was the old behavior), the wipe reports itself so the caller can rebind
 * to the new account's own database, and the logout path's wipeLocal() is
 * AWAITED to completion — a fire-and-forget clear raced the redirect.
 */
import { describe, expect, it } from 'vitest';

import { createServerHistoryStore, type StorageLike } from '../lib/history';
import { userDbName, type CachePersister } from '../lib/idbCache';
import type { HistoryApi } from '../lib/historyApi';
import { PREFS_STORAGE_KEY, savePrefs, DEFAULT_PREFS } from '../lib/prefs';
import { FEEDBACK_STORAGE_KEY, saveFeedback } from '../lib/feedback';

function makeStorage(): StorageLike & { keys(): string[] } {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
    keys: () => [...map.keys()],
  };
}

/** HistoryApi where every call quietly succeeds (sync is not under test). */
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
  cleared: number;
  /** Set true while clear() is still pending — proves wipeLocal awaited it. */
  clearing: boolean;
}

function makePersister(): FakePersister {
  const p: FakePersister = {
    mode: 'async',
    cleared: 0,
    clearing: false,
    loadAll: async () => [],
    put: async () => undefined,
    remove: async () => undefined,
    clear: async () => {
      p.clearing = true;
      await new Promise((r) => setTimeout(r, 5));
      p.clearing = false;
      p.cleared += 1;
    },
  };
  return p;
}

function seedAccountData(storage: StorageLike) {
  savePrefs(storage, 'conv-1', { ...DEFAULT_PREFS, model: 'fast' });
  saveFeedback(storage, 'msg-1', 'up');
}

describe('setActiveUser — id-keyed scoping', () => {
  it('first bind records the key without wiping anything', async () => {
    const storage = makeStorage();
    const store = createServerHistoryStore({ storage, api: quietApi() });
    const conv = store.create('pre-auth chat');
    seedAccountData(storage);

    expect(store.setActiveUser('u7')).toBe(false);
    expect(store.get(conv.id)).not.toBeNull();
    expect(storage.getItem(PREFS_STORAGE_KEY)).not.toBeNull();
    // First login still migrates: the pre-auth data belongs to this account.
    expect(await store.migrateLocalConversations()).toBe(0); // no messages
  });

  it('rebinding the same key is a no-op', () => {
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: quietApi(),
    });
    store.setActiveUser('u7');
    store.create('kept');
    expect(store.setActiveUser('u7')).toBe(false);
    expect(store.list()).toHaveLength(1);
  });

  it('a DIFFERENT account wipes conversations, prefs and thumbs', async () => {
    const storage = makeStorage();
    const persister = makePersister();
    const store = createServerHistoryStore({
      storage,
      api: quietApi(),
      persister,
    });
    await store.ready();
    store.setActiveUser('u7');
    const conv = store.create('u7 private');
    store.saveMessages(conv.id, [
      { id: 'm1', role: 'user', content: 'secret', createdAt: 1 },
    ]);
    await store.flush();
    seedAccountData(storage);

    expect(store.setActiveUser('u8')).toBe(true);
    expect(store.list()).toHaveLength(0);
    expect(storage.getItem(PREFS_STORAGE_KEY)).toBeNull();
    expect(storage.getItem(FEEDBACK_STORAGE_KEY)).toBeNull();
    expect(storage.getItem('techsara.history.v1')).toBeNull();
    await store.flush();
    expect(persister.cleared).toBeGreaterThanOrEqual(1);
    // Nothing of u7's is donated to u8 either.
    expect(await store.migrateLocalConversations()).toBe(0);
  });
});

describe('wipeLocal — the logout teardown', () => {
  it('erases every account-scoped key and AWAITS the persister clear', async () => {
    const storage = makeStorage();
    const persister = makePersister();
    const store = createServerHistoryStore({
      storage,
      api: quietApi(),
      persister,
    });
    await store.ready();
    store.setActiveUser('u7');
    store.create('to be erased');
    await store.flush();
    seedAccountData(storage);

    await store.wipeLocal();

    expect(persister.cleared).toBe(1);
    expect(persister.clearing).toBe(false); // resolved AFTER the clear landed
    expect(store.list()).toHaveLength(0);
    expect(storage.getItem('techsara.history.sync.v1')).toBeNull();
    expect(storage.getItem('techsara.history.v1')).toBeNull();
    expect(storage.getItem(PREFS_STORAGE_KEY)).toBeNull();
    expect(storage.getItem(FEEDBACK_STORAGE_KEY)).toBeNull();
  });

  it('settles an in-flight debounced write before resolving', async () => {
    const persister = makePersister();
    const events: string[] = [];
    persister.put = async () => {
      await new Promise((r) => setTimeout(r, 5));
      events.push('put');
    };
    const baseClear = persister.clear.bind(persister);
    persister.clear = async () => {
      await baseClear();
      events.push('clear');
    };
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: quietApi(),
      persister,
    });
    await store.ready();
    store.create('written moments before logout');
    await store.flush(); // the put is on the chain

    await store.wipeLocal();
    // The wipe's clear ran after any tracked write — nothing the user asked
    // to be rid of can re-materialize behind the redirect.
    expect(events.at(-1)).toBe('clear');
  });
});

describe('per-user database naming', () => {
  it('derives techsara-history:u<id> from the scope key', () => {
    expect(userDbName('u7')).toBe('techsara-history:u7');
  });
});
