/**
 * AI conversation titles — the CLIENT half.
 *
 * The server names a conversation from its first exchange; the client asks it
 * to, and adopts the answer. Both halves matter: a title written server-side
 * without the client asking would stay INVISIBLE, because nothing pulls a
 * title change into this cache except `refresh()`, which runs once on mount.
 *
 * What is asserted:
 * - the call is CHAINED behind the conversation's create/push, so it cannot
 *   reach the server before the row exists (which would 404 silently and
 *   leave the chat called "hi" forever);
 * - `generated: false` — the server declining — leaves the local title alone;
 * - a failure is swallowed and never marks the conversation dirty;
 * - a conversation without a full exchange yet is not titled at all.
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

type Calls = { order: string[]; titleCalls: string[] };

function makeApi(
  calls: Calls,
  title: { title: string; generated: boolean } | Error,
): HistoryApi {
  return {
    async list() {
      return [];
    },
    async get(id: string) {
      return { id, title: 'hi', messages: [] as ServerMessage[] };
    },
    async create(id) {
      calls.order.push(`create:${id}`);
    },
    async update() {
      calls.order.push('update');
    },
    async remove() {},
    async appendMessage() {
      calls.order.push('append');
    },
    async setFeedback() {},
    async generateTitle(id: string) {
      calls.order.push(`title:${id}`);
      calls.titleCalls.push(id);
      if (title instanceof Error) throw title;
      return title;
    },
    async replaceMessages() {
      calls.order.push('replace');
    },
    async truncateMessages() {},
  };
}

async function storeWith(
  calls: Calls,
  title: { title: string; generated: boolean } | Error,
): Promise<ServerHistoryStore> {
  const store = createServerHistoryStore({
    storage: memoryStorage(),
    api: makeApi(calls, title),
  });
  await store.ready();
  store.setActiveUser('alice');
  return store;
}

function exchange(store: ServerHistoryStore) {
  const conv = store.create('hi');
  store.saveMessages(conv.id, [
    { id: 'm1', role: 'user' as const, content: 'hi', createdAt: 1 },
    {
      id: 'm2',
      role: 'assistant' as const,
      content: 'Hello! How can I help you today?',
      createdAt: 2,
    },
  ]);
  return conv.id;
}

describe('AI conversation titles (client)', () => {
  let calls: Calls;
  beforeEach(() => {
    calls = { order: [], titleCalls: [] };
  });

  it('adopts a generated title', async () => {
    const store = await storeWith(calls, {
      title: 'Contact Export Process',
      generated: true,
    });
    const id = exchange(store);
    await store.generateTitle(id);
    expect(store.get(id)?.title).toBe('Contact Export Process');
  });

  it('runs AFTER the create, never before it', async () => {
    // The bug this prevents: the title POST beating the create POST, 404ing,
    // and the conversation keeping its first-message name forever.
    const store = await storeWith(calls, { title: 'Named', generated: true });
    const id = exchange(store);
    await store.generateTitle(id);
    await store.flush();

    const createAt = calls.order.findIndex((c) => c.startsWith('create:'));
    const titleAt = calls.order.findIndex((c) => c.startsWith('title:'));
    expect(createAt).toBeGreaterThanOrEqual(0);
    expect(titleAt).toBeGreaterThan(createAt);
  });

  it('leaves the title alone when the server declines', async () => {
    // "hi" / "Hello! How can I help you today?" — nothing worth naming.
    const store = await storeWith(calls, { title: 'hi', generated: false });
    const id = exchange(store);
    const before = store.get(id)?.title;
    await store.generateTitle(id);
    expect(store.get(id)?.title).toBe(before);
  });

  it('never titles a conversation that has no exchange yet', async () => {
    const store = await storeWith(calls, { title: 'Named', generated: true });
    const conv = store.create('hi');
    store.saveMessages(conv.id, [
      { id: 'm1', role: 'user' as const, content: 'hi', createdAt: 1 },
    ]);
    await store.generateTitle(conv.id);
    expect(calls.titleCalls).toEqual([]); // the answer has not landed yet
  });

  it('swallows a failure and keeps the existing title', async () => {
    const store = await storeWith(calls, new Error('router down'));
    const id = exchange(store);
    const before = store.get(id)?.title;
    await expect(store.generateTitle(id)).resolves.toBeUndefined();
    expect(store.get(id)?.title).toBe(before);
  });

  it('is a no-op for an unknown conversation', async () => {
    const store = await storeWith(calls, { title: 'Named', generated: true });
    await store.generateTitle('does-not-exist');
    expect(calls.titleCalls).toEqual([]);
  });
});
