/**
 * Thumbs are stored SERVER-SIDE since 2026-08-11.
 *
 * The bug this fixes was silent: feedback lived in localStorage keyed by the
 * client message id, but a live message carries a random uuid while the same
 * message rehydrated from the server is keyed positionally
 * (`srv-<conversation>-<index>`). So a thumb given to a fresh reply simply
 * vanished on the next reload, and nobody noticed because the icon just came
 * back empty.
 *
 * What is asserted here:
 * - a stored thumb is hydrated onto the message (survives reload);
 * - clicking sends it to the server, keyed by the SERVER id;
 * - a message with no server row yet degrades to cache-only, and rides to the
 *   server on the next sync rather than being dropped;
 * - a failed request never throws at the caller (a thumb must not raise a
 *   red pill) and leaves the value in the cache;
 * - the value is carried in the whole-thread push, so a re-sync cannot
 *   resurrect a thumb the user has cleared.
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { createServerHistoryStore, type ServerHistoryStore } from '../lib/history';
import type { HistoryApi, ServerMessage } from '../lib/historyApi';

function memoryStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
}

type Recorded = {
  feedback: Array<[string, number, 'up' | 'down' | null]>;
  replaced: ServerMessage[][];
};

function makeApi(
  serverMessages: ServerMessage[],
  recorded: Recorded,
  opts: { failFeedback?: boolean } = {},
): HistoryApi {
  return {
    async list() {
      return [
        {
          id: 'c1',
          title: 'chat',
          updated_at: '2026-08-11T00:00:00+00:00',
          created_at: '2026-08-11T00:00:00+00:00',
          pinned: false,
          archived: false,
        },
      ];
    },
    async get(id: string) {
      return { id, title: 'chat', messages: serverMessages };
    },
    async create() {},
    async update() {},
    async remove() {},
    async appendMessage() {
      return { id: 99 };
    },
    async generateTitle() {
      return { title: '', generated: false };
    },
    async setFeedback(id, messageId, feedback) {
      if (opts.failFeedback) throw new Error('offline');
      recorded.feedback.push([id, messageId, feedback]);
    },
    async replaceMessages(_id, messages) {
      recorded.replaced.push(messages);
    },
    async truncateMessages() {},
  };
}

async function storeWith(
  serverMessages: ServerMessage[],
  recorded: Recorded,
  opts: { failFeedback?: boolean } = {},
): Promise<ServerHistoryStore> {
  const store = createServerHistoryStore({
    storage: memoryStorage(),
    api: makeApi(serverMessages, recorded, opts),
  });
  await store.ready();
  store.setActiveUser('alice');
  await store.refresh();
  return store;
}

const THREAD: ServerMessage[] = [
  { id: 11, role: 'user', content: 'question', meta: null, feedback: null },
  { id: 12, role: 'assistant', content: 'answer', meta: null, feedback: 'up' },
];

describe('server-stored message feedback', () => {
  let recorded: Recorded;
  beforeEach(() => {
    recorded = { feedback: [], replaced: [] };
  });

  it('hydrates a stored thumb onto the message — this is what survives a reload', async () => {
    const store = await storeWith(THREAD, recorded);
    const conv = await store.load('c1');
    expect(conv?.messages.map((m) => m.feedback)).toEqual([null, 'up']);
  });

  it('keeps the server id, which is the only stable handle on a message', async () => {
    const store = await storeWith(THREAD, recorded);
    const conv = await store.load('c1');
    expect(conv?.messages.map((m) => m.serverId)).toEqual([11, 12]);
    // …and the client id is NOT the server id: that mismatch is the bug.
    expect(conv?.messages.map((m) => m.id)).toEqual(['srv-c1-0', 'srv-c1-1']);
  });

  it('sends a thumb to the server keyed by the SERVER id', async () => {
    const store = await storeWith(THREAD, recorded);
    await store.load('c1');
    await store.setMessageFeedback('c1', 'srv-c1-1', 'down');

    expect(recorded.feedback).toEqual([['c1', 12, 'down']]);
    expect(store.get('c1')?.messages[1].feedback).toBe('down');
  });

  it('clearing sends null rather than omitting the call', async () => {
    const store = await storeWith(THREAD, recorded);
    await store.load('c1');
    await store.setMessageFeedback('c1', 'srv-c1-1', null);
    expect(recorded.feedback).toEqual([['c1', 12, null]]);
    expect(store.get('c1')?.messages[1].feedback).toBeNull();
  });

  it('a message with no server row yet is cached, not dropped', async () => {
    // No `id` from the server → no serverId → nothing to address remotely.
    const store = await storeWith(
      [{ role: 'assistant', content: 'fresh', meta: null }],
      recorded,
    );
    await store.load('c1');
    await store.setMessageFeedback('c1', 'srv-c1-0', 'up');

    expect(recorded.feedback).toEqual([]); // no call attempted
    expect(store.get('c1')?.messages[0].feedback).toBe('up'); // but not lost
  });

  it('a failed request does not throw and keeps the value locally', async () => {
    const store = await storeWith(THREAD, recorded, { failFeedback: true });
    await store.load('c1');
    await expect(
      store.setMessageFeedback('c1', 'srv-c1-1', 'down'),
    ).resolves.toBeUndefined();
    expect(store.get('c1')?.messages[1].feedback).toBe('down');
  });

  it('carries the thumb in a whole-thread push, so a re-sync cannot resurrect a cleared one', async () => {
    const store = await storeWith(THREAD, recorded);
    const conv = await store.load('c1');
    await store.setMessageFeedback('c1', 'srv-c1-1', null);

    // Force the diverged-tail path. APPENDING is not enough: the sync sees
    // the pushed ids as a prefix and takes the cheap append route. A
    // regenerate REPLACES the last turn with a differently-identified one,
    // which is the case that rewrites the whole thread server-side — and
    // therefore the case that could lose a thumb.
    const messages = conv?.messages ?? [];
    store.saveMessages('c1', [
      messages[0],
      {
        ...messages[1],
        id: 'regenerated',
        content: 'a better answer',
        feedback: null,
      },
    ]);
    await store.flush();

    const pushed = recorded.replaced.at(-1);
    expect(pushed).toBeDefined();
    // The cleared thumb travels as an explicit null, not as an omission that
    // the server would fill back in from the row it is about to delete.
    expect(pushed?.[1].feedback).toBeNull();
  });

  it('learns the server id from the append response', async () => {
    // The case that actually broke in the browser: a conversation you are
    // chatting in is never re-fetched (the cache is in sync), so the append
    // response is the ONLY place its messages can pick up a server id.
    const store = await storeWith(THREAD, recorded);
    const conv = await store.load('c1');
    store.saveMessages('c1', [
      ...(conv?.messages ?? []),
      {
        id: 'brand-new',
        role: 'assistant' as const,
        content: 'a fresh reply',
        createdAt: Date.now(),
      },
    ]);
    await store.flush();

    expect(store.get('c1')?.messages.at(-1)?.serverId).toBe(99);

    // …and that id is what the thumb is sent with.
    await store.setMessageFeedback('c1', 'brand-new', 'up');
    expect(recorded.feedback).toEqual([['c1', 99, 'up']]);
  });

  it('resolves a missing server id by re-reading the server, matching by position', async () => {
    // A cached message from before ids were tracked: no serverId, but the
    // server does have the row. It must not silently stay client-side.
    const store = await storeWith(THREAD, recorded);
    await store.load('c1');
    store.saveMessages('c1', [
      { id: 'legacy-0', role: 'user' as const, content: 'question', createdAt: 1 },
      { id: 'legacy-1', role: 'assistant' as const, content: 'answer', createdAt: 2 },
    ]);

    await store.setMessageFeedback('c1', 'legacy-1', 'down');
    // Position 1 on the server is row 12.
    expect(recorded.feedback).toEqual([['c1', 12, 'down']]);
    expect(store.get('c1')?.messages[1].feedback).toBe('down');
  });

  it('is a no-op for an unknown conversation or message', async () => {
    const store = await storeWith(THREAD, recorded);
    await store.load('c1');
    await store.setMessageFeedback('nope', 'srv-c1-1', 'up');
    await store.setMessageFeedback('c1', 'not-a-message', 'up');
    expect(recorded.feedback).toEqual([]);
  });
});
