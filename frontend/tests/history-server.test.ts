import { describe, expect, it } from 'vitest';
import {
  createServerHistoryStore,
  type StorageLike,
} from '../lib/history';
import {
  HistoryApiError,
  type HistoryApi,
  type ServerMessage,
} from '../lib/historyApi';
import type { ChatMessage } from '../lib/types';

/* ------------------------------------------------------------- fakes */

function makeStorage(): StorageLike {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

interface FakeConversation {
  title: string;
  messages: ServerMessage[];
  /** V3 §1 flags, defaulting to 0/false exactly like the SQLite columns. */
  pinned?: boolean;
  archived?: boolean;
  updated_at?: string;
}

interface FakeServer {
  api: HistoryApi;
  convs: Map<string, FakeConversation>;
  calls: string[];
  /** When true every call throws like the network is down. */
  offline: boolean;
  setOffline(v: boolean): void;
}

function makeServer(): FakeServer {
  const convs = new Map<string, FakeConversation>();
  const calls: string[] = [];
  const state = { offline: false };
  function guard(op: string) {
    calls.push(op);
    if (state.offline) throw new HistoryApiError(0, 'offline');
  }
  const api: HistoryApi = {
    async list(options) {
      const wantArchived = options?.archived === true;
      guard(wantArchived ? 'list:archived' : 'list');
      return [...convs.entries()]
        .filter(([, c]) => (c.archived === true) === wantArchived)
        .map(([id, c]) => ({
          id,
          title: c.title,
          updated_at: c.updated_at ?? '2026-07-22 10:00:00',
          created_at: '2026-07-22 09:00:00',
          pinned: c.pinned === true,
          archived: c.archived === true,
        }))
        // V3 §1: pinned first, then updated_at descending.
        .sort(
          (a, b) =>
            Number(b.pinned) - Number(a.pinned) ||
            String(b.updated_at).localeCompare(String(a.updated_at)),
        );
    },
    async get(id) {
      guard(`get:${id}`);
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      return { id, title: c.title, messages: c.messages };
    },
    async create(id, title) {
      guard(`create:${id ?? '?'}`);
      // Matches the real server: a duplicate id is a 409, not an overwrite
      // (orchestrator/app/history.py create_conversation).
      if (id !== undefined && convs.has(id)) {
        throw new HistoryApiError(409, 'conversation id already exists');
      }
      convs.set(id ?? `gen-${convs.size}`, { title, messages: [] });
    },
    async update(id, patch) {
      guard(`update:${id}`);
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      if (patch.title !== undefined) c.title = patch.title;
      if (patch.pinned !== undefined) c.pinned = patch.pinned;
      if (patch.archived !== undefined) c.archived = patch.archived;
    },
    async remove(id) {
      guard(`remove:${id}`);
      if (!convs.delete(id)) throw new HistoryApiError(404, 'not found');
    },
    async appendMessage(id, message) {
      guard(`append:${id}`);
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      c.messages.push(message);
    },
    async truncateMessages(id, keep, expectedTotal) {
      guard(`truncate:${id}`);
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      // Optimistic concurrency, exactly like the real endpoint.
      if (c.messages.length !== expectedTotal) {
        throw new HistoryApiError(409, 'conversation changed');
      }
      c.messages = c.messages.slice(0, keep);
    },
    async setFeedback(id, messageId, feedback) {
      guard('setFeedback');
      const conv = convs.get(id);
      const target = conv?.messages?.find(
        (m: ServerMessage) => m.id === messageId,
      );
      if (!target) throw new HistoryApiError(404, 'message not found');
      target.feedback = feedback;
    },
    async replaceMessages(id, messages) {
      guard(`replace:${id}`);
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      // The server-side invariant: a sync may never REDUCE the message count.
      if (messages.length < c.messages.length) {
        throw new HistoryApiError(409, 'refusing to shrink conversation');
      }
      c.messages = [...messages];
    },
  };
  return {
    api,
    convs,
    calls,
    get offline() {
      return state.offline;
    },
    setOffline(v: boolean) {
      state.offline = v;
    },
  };
}

function msg(role: 'user' | 'assistant', content: string): ChatMessage {
  return {
    id: `${role}-${content}-${Math.random().toString(36).slice(2, 8)}`,
    role,
    content,
    createdAt: Date.now(),
  };
}

/* -------------------------------------------------------------- tests */

describe('server history store: write-through (V2 §4b)', () => {
  it('creates conversations on the server in the background', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('pipeline question');
    await store.flush();
    expect(server.convs.has(conv.id)).toBe(true);
    expect(server.convs.get(conv.id)?.title).toBe('pipeline question');
  });

  it('appends only the new messages on a pure-append save', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('q');
    const u = msg('user', 'q');
    const a = { ...msg('assistant', 'a'), meta: { route: 'sql' as const, sql: 'SELECT 1' } };
    store.saveMessages(conv.id, [u]);
    store.saveMessages(conv.id, [u, a]);
    await store.flush();
    const remote = server.convs.get(conv.id)!;
    expect(remote.messages.map((m) => m.content)).toEqual(['q', 'a']);
    expect(remote.messages[1].meta).toMatchObject({ sql: 'SELECT 1' });

    // The saves above land while the first sync is still queued, so they
    // collapse into one atomic upload. What matters is the NEXT turn: it must
    // go up as a single incremental append, never re-uploading the thread and
    // never taking the rebuild path.
    server.calls.length = 0;
    const u2 = msg('user', 'follow-up');
    store.saveMessages(conv.id, [u, a, u2]);
    await store.flush();
    expect(server.calls.filter((c) => c === `append:${conv.id}`)).toHaveLength(1);
    expect(server.calls.filter((c) => c.startsWith('replace:'))).toHaveLength(0);
    expect(server.calls.filter((c) => c.startsWith('remove:'))).toHaveLength(0);
    expect(remote.messages.map((m) => m.content)).toEqual(['q', 'a', 'follow-up']);
  });

  it('rebuilds the server copy when the tail diverges (regenerate)', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('q');
    const u = msg('user', 'q');
    const a1 = msg('assistant', 'first answer');
    store.saveMessages(conv.id, [u, a1]);
    await store.flush();
    const a2 = msg('assistant', 'regenerated answer');
    store.saveMessages(conv.id, [u, a2]); // diverged tail
    await store.flush();
    expect(
      server.convs.get(conv.id)?.messages.map((m) => m.content),
    ).toEqual(['q', 'regenerated answer']);
  });

  it('renames on the server too', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('old');
    await store.flush();
    store.rename(conv.id, 'Pipeline deep dive');
    await store.flush();
    expect(server.convs.get(conv.id)?.title).toBe('Pipeline deep dive');
  });

  it('deletes on the server too', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('bye');
    await store.flush();
    store.remove(conv.id);
    await store.flush();
    expect(server.convs.has(conv.id)).toBe(false);
    expect(store.get(conv.id)).toBeNull();
  });
});

describe('server history store: offline cache behavior (V2 §4b)', () => {
  it('keeps working offline and pushes dirty conversations on refresh()', async () => {
    const server = makeServer();
    const storage = makeStorage();
    const store = createServerHistoryStore({ storage, api: server.api });

    server.setOffline(true);
    const conv = store.create('offline question');
    const u = msg('user', 'offline question');
    store.saveMessages(conv.id, [u]);
    await store.flush();
    expect(server.convs.has(conv.id)).toBe(false);
    expect(store.get(conv.id)?.messages).toHaveLength(1); // cache intact

    expect(await store.refresh()).toBe(false); // still offline

    server.setOffline(false);
    expect(await store.refresh()).toBe(true);
    expect(server.convs.get(conv.id)?.messages.map((m) => m.content)).toEqual([
      'offline question',
    ]);
  });

  it('completes pending deletes on refresh()', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('to delete');
    await store.flush();
    server.setOffline(true);
    store.remove(conv.id);
    await store.flush();
    expect(server.convs.has(conv.id)).toBe(true); // delete never landed
    server.setOffline(false);
    await store.refresh();
    expect(server.convs.has(conv.id)).toBe(false);
  });

  it('drops locally cached conversations that were deleted on the server', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('deleted elsewhere');
    await store.flush();
    server.convs.delete(conv.id); // another session deleted it
    await store.refresh();
    expect(store.get(conv.id)).toBeNull();
  });
});

describe('server history store: pull + lazy load (V2 §4b)', () => {
  it('refresh() lists server conversations into the cache; load() fetches messages', async () => {
    const server = makeServer();
    server.convs.set('remote-1', {
      title: 'From another device',
      messages: [
        { role: 'user', content: 'hello', meta: null },
        {
          role: 'assistant',
          content: 'hi there',
          meta: { route: 'chat', reasoning: 'stored thought', reasoning_seconds: 3 },
        },
      ],
    });
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    await store.refresh();
    const summaries = store.list();
    expect(summaries.map((s) => s.id)).toContain('remote-1');
    expect(store.get('remote-1')?.messages).toHaveLength(0); // lazy

    const loaded = await store.load('remote-1');
    expect(loaded?.messages).toHaveLength(2);
    expect(loaded?.messages[1].meta?.reasoning).toBe('stored thought');
    expect(loaded?.messages[1].meta?.reasoning_seconds).toBe(3);
    // Cached now — a second load with the server offline still works.
    server.setOffline(true);
    const again = await store.load('remote-1');
    expect(again?.messages).toHaveLength(2);
  });

  it('appending after a server load does not duplicate loaded messages', async () => {
    const server = makeServer();
    server.convs.set('remote-2', {
      title: 'Continue me',
      messages: [{ role: 'user', content: 'first', meta: null }],
    });
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    await store.refresh();
    const loaded = await store.load('remote-2');
    store.saveMessages('remote-2', [...loaded!.messages, msg('user', 'second')]);
    await store.flush();
    expect(
      server.convs.get('remote-2')?.messages.map((m) => m.content),
    ).toEqual(['first', 'second']);
  });
});

describe('server history store: one-time migration (V2 §4b)', () => {
  it('uploads pre-auth local conversations once, then never again', async () => {
    const storage = makeStorage();
    // Seed v1-style local history BEFORE the server store exists.
    const seedConv = {
      id: 'local-old',
      title: 'Pre-login chat',
      createdAt: 1,
      updatedAt: 2,
      messages: [
        { id: 'm1', role: 'user', content: 'old question', createdAt: 1 },
        {
          id: 'm2',
          role: 'assistant',
          content: 'old answer',
          meta: { route: 'rag' },
          createdAt: 2,
        },
      ],
    };
    storage.setItem('techsara.history.v1', JSON.stringify([seedConv]));

    const server = makeServer();
    const store = createServerHistoryStore({ storage, api: server.api });
    store.setActiveUser('naman');
    const migrated = await store.migrateLocalConversations();
    expect(migrated).toBe(1);
    expect(server.convs.get('local-old')?.messages.map((m) => m.content)).toEqual(
      ['old question', 'old answer'],
    );

    // Second call is a no-op (the migrated flag is persisted).
    expect(await store.migrateLocalConversations()).toBe(0);
  });

  it('does not mark migration done when the server is unreachable', async () => {
    const storage = makeStorage();
    storage.setItem(
      'techsara.history.v1',
      JSON.stringify([
        {
          id: 'local-1',
          title: 't',
          createdAt: 1,
          updatedAt: 2,
          messages: [{ id: 'm1', role: 'user', content: 'q', createdAt: 1 }],
        },
      ]),
    );
    const server = makeServer();
    server.setOffline(true);
    const store = createServerHistoryStore({ storage, api: server.api });
    store.setActiveUser('naman');
    await expect(store.migrateLocalConversations()).rejects.toThrow();
    server.setOffline(false);
    expect(await store.migrateLocalConversations()).toBe(1);
    expect(server.convs.has('local-1')).toBe(true);
  });
});

describe('server history store: pin + archive (V3 §2)', () => {
  it('keeps archived conversations out of the default list', () => {
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: makeServer().api,
    });
    const active = store.create('still working on this');
    const old = store.create('done with this');
    store.setArchived(old.id, true);

    expect(store.list().map((c) => c.id)).toEqual([active.id]);
    expect(store.listArchived().map((c) => c.id)).toEqual([old.id]);
    expect(store.get(old.id)?.archived).toBe(true); // still readable
  });

  it('floats pinned conversations to the top of the list', () => {
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: makeServer().api,
    });
    const first = store.create('oldest');
    store.create('middle');
    const newest = store.create('newest');
    expect(store.list()[0].id).toBe(newest.id);

    store.setPinned(first.id, true);
    expect(store.list().map((c) => c.title)).toEqual([
      'oldest',
      'newest',
      'middle',
    ]);
    expect(store.list()[0].pinned).toBe(true);

    store.setPinned(first.id, false);
    expect(store.list()[0].id).toBe(newest.id);
  });

  it('sets the flag only — archiving never reorders by recency', () => {
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: makeServer().api,
    });
    const conv = store.create('leave my timestamp alone');
    const before = store.list()[0].updatedAt;
    store.setArchived(conv.id, true);
    store.setPinned(conv.id, true);
    expect(store.listArchived()[0].updatedAt).toBe(before);
  });

  it('round-trips both flags to the server', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('sync me');
    await store.flush();

    store.setPinned(conv.id, true);
    store.setArchived(conv.id, true);
    await store.flush();
    expect(server.convs.get(conv.id)?.pinned).toBe(true);
    expect(server.convs.get(conv.id)?.archived).toBe(true);

    store.setPinned(conv.id, false);
    store.setArchived(conv.id, false);
    await store.flush();
    expect(server.convs.get(conv.id)?.pinned).toBe(false);
    expect(server.convs.get(conv.id)?.archived).toBe(false);
  });

  it('re-applies the flags when the server copy is rebuilt', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('q');
    const u = msg('user', 'q');
    store.saveMessages(conv.id, [u, msg('assistant', 'first')]);
    store.setPinned(conv.id, true);
    await store.flush();

    // A regenerate diverges the tail, so the whole conversation is re-pushed
    // (delete + recreate) — the pin must not be lost in the rebuild.
    store.saveMessages(conv.id, [u, msg('assistant', 'regenerated')]);
    await store.flush();
    expect(server.convs.get(conv.id)?.pinned).toBe(true);
  });

  it('retries a flag that could not be pushed while offline', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('pin me later');
    await store.flush();

    server.setOffline(true);
    store.setPinned(conv.id, true);
    await store.flush();
    expect(server.convs.get(conv.id)?.pinned).toBeFalsy(); // never landed

    server.setOffline(false);
    expect(await store.refresh()).toBe(true);
    expect(server.convs.get(conv.id)?.pinned).toBe(true);
  });

  it('pulls archived rows from the server without hiding or dropping them', async () => {
    const server = makeServer();
    server.convs.set('remote-archived', {
      title: 'Last quarter',
      messages: [],
      archived: true,
    });
    server.convs.set('remote-pinned', {
      title: 'Board deck',
      messages: [],
      pinned: true,
    });
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    await store.refresh();

    expect(store.list().map((c) => c.id)).toEqual(['remote-pinned']);
    expect(store.list()[0].pinned).toBe(true);
    expect(store.listArchived().map((c) => c.id)).toEqual(['remote-archived']);
  });

  it('does not treat a locally archived conversation as deleted elsewhere', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('archive then refresh');
    await store.flush();
    store.setArchived(conv.id, true);
    await store.flush();

    await store.refresh();
    expect(store.listArchived().map((c) => c.id)).toEqual([conv.id]);
    expect(store.get(conv.id)).not.toBeNull();
  });

  it('keeps archived conversations when the backend rejects ?archived', async () => {
    const server = makeServer();
    const preV3: HistoryApi = {
      ...server.api,
      async list(options) {
        if (options?.archived) throw new HistoryApiError(422, 'unsupported');
        return server.api.list(options);
      },
    };
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: preV3,
    });
    const conv = store.create('archived locally');
    await store.flush();
    store.setArchived(conv.id, true);
    await store.flush();

    expect(await store.refresh()).toBe(true);
    expect(store.listArchived().map((c) => c.id)).toEqual([conv.id]);
  });

  it('refreshArchived() lazily pulls the archived list', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    server.convs.set('arch-1', {
      title: 'Filed away',
      messages: [],
      archived: true,
    });
    expect(store.listArchived()).toHaveLength(0);

    expect(await store.refreshArchived()).toBe(true);
    expect(store.listArchived().map((c) => c.title)).toEqual(['Filed away']);
    expect(server.calls).toContain('list:archived');

    server.setOffline(true);
    expect(await store.refreshArchived()).toBe(false); // cache still stands
    expect(store.listArchived()).toHaveLength(1);
  });
});

describe('server history store: export (V3 §2)', () => {
  it('loads the conversation and builds <slug>-<id>.md', async () => {
    const server = makeServer();
    server.convs.set('remote-export', {
      title: 'Pipeline by owner',
      messages: [
        { role: 'user', content: 'open pipeline by owner', meta: null },
        {
          role: 'assistant',
          content: 'Here you go.',
          meta: {
            route: 'sql',
            sql: 'SELECT 1',
            citations: [
              { record_id: '0011x', object: 'Account', url: 'https://x/0011x' },
            ],
          },
        },
      ],
    });
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    await store.refresh();

    const file = await store.exportMarkdown('remote-export');
    expect(file?.filename).toBe('pipeline-by-owner-remote-export.md');
    expect(file?.markdown).toContain('# Pipeline by owner');
    expect(file?.markdown).toContain('## You\n\nopen pipeline by owner');
    expect(file?.markdown).toContain('```sql\nSELECT 1\n```');
    expect(file?.markdown).toContain('**Records:** 0011x');
  });

  it('returns null for a conversation that no longer exists', async () => {
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    expect(await store.exportMarkdown('missing')).toBeNull();
  });
});

describe('server history store: account switching (V2 §4a)', () => {
  it('clears the cache when a different user signs in on this browser', async () => {
    const storage = makeStorage();
    const server = makeServer();
    const store = createServerHistoryStore({ storage, api: server.api });
    store.setActiveUser('alice');
    const conv = store.create('alice private');
    store.saveMessages(conv.id, [msg('user', 'alice private')]);
    await store.flush();

    store.setActiveUser('bob');
    expect(store.list()).toHaveLength(0); // nothing of alice's is visible
    // …and nothing of alice's gets migrated/pushed for bob.
    expect(await store.migrateLocalConversations()).toBe(0);
  });

  it('keeps the cache when the same user signs in again', async () => {
    const storage = makeStorage();
    const server = makeServer();
    const store = createServerHistoryStore({ storage, api: server.api });
    store.setActiveUser('alice');
    store.create('kept');
    await store.flush();
    store.setActiveUser('alice');
    expect(store.list().map((c) => c.title)).toEqual(['kept']);
  });
});

/* ------------------------------------------- Phase 0-critical regressions */

describe('conversation-destroying sync (regression)', () => {
  it('NEVER shrinks a conversation when the local cache is empty', async () => {
    // The exact reported path: the server holds a long thread, this browser
    // has the conversation cached with NO messages (listed by refresh() but
    // never opened here, or evicted by a quota purge), and a finished stream
    // saves just the replayed answer. The old code DELETEd and recreated the
    // conversation from that single message, destroying every earlier turn.
    const server = makeServer();
    await server.api.create('c1', 'long chat');
    const remote = server.convs.get('c1')!;
    remote.messages = [
      { role: 'user', content: 'turn 1', meta: null },
      { role: 'assistant', content: 'answer 1', meta: null },
      { role: 'user', content: 'turn 2', meta: null },
    ];

    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    await store.refresh(); // caches c1 with messages: [] and pushed 'unknown'

    // A stream finishes and writes back only what it had locally.
    store.saveMessages('c1', [msg('assistant', 'replayed answer')]);
    await store.flush();

    // The server copy must be intact — never deleted, never shortened.
    expect(server.calls.filter((c) => c.startsWith('remove:'))).toHaveLength(0);
    expect(server.convs.get('c1')).toBeDefined();
    expect(server.convs.get('c1')!.messages.map((m) => m.content)).toEqual([
      'turn 1',
      'answer 1',
      'turn 2',
    ]);
  });

  it('adopts server truth after a refused shrink instead of retrying forever', async () => {
    const server = makeServer();
    await server.api.create('c2', 'chat');
    server.convs.get('c2')!.messages = [
      { role: 'user', content: 'kept', meta: null },
      { role: 'assistant', content: 'also kept', meta: null },
    ];
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    await store.refresh();
    store.saveMessages('c2', [msg('assistant', 'short')]);
    await store.flush();

    // The refused push pulls the server's version into the cache.
    const cached = store.get('c2');
    expect(cached?.messages.map((m) => m.content)).toEqual(['kept', 'also kept']);
  });

  it('still rebuilds when the tail diverges but the thread does not shrink', async () => {
    // Regenerate replaces the last answer: same length, different tail. That
    // is legitimate and must still reach the server.
    const server = makeServer();
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    const conv = store.create('q');
    const u = msg('user', 'q');
    store.saveMessages(conv.id, [u, msg('assistant', 'first')]);
    await store.flush();
    store.saveMessages(conv.id, [u, msg('assistant', 'regenerated')]);
    await store.flush();
    expect(
      server.convs.get(conv.id)!.messages.map((m) => m.content),
    ).toEqual(['q', 'regenerated']);
  });
});

describe('the one sanctioned shrink: confirmed regenerate', () => {
  it('truncates on the server and leaves the sync path able to append', async () => {
    const server = makeServer();
    await server.api.create('r1', 'chat');
    server.convs.get('r1')!.messages = [
      { role: 'user', content: 'q1', meta: null },
      { role: 'assistant', content: 'a1', meta: null },
      { role: 'user', content: 'q2', meta: null },
      { role: 'assistant', content: 'a2', meta: null },
    ];
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    await store.refresh();
    const loaded = await store.load('r1');
    expect(loaded!.messages).toHaveLength(4);

    // User confirmed regenerating the FIRST answer: keep [q1].
    await store.truncateMessages('r1', 1);
    expect(server.convs.get('r1')!.messages.map((m) => m.content)).toEqual(['q1']);

    // The regenerated answer then appends through the ordinary path.
    const kept = store.get('r1')!.messages;
    store.saveMessages('r1', [...kept, msg('assistant', 'regenerated')]);
    await store.flush();
    expect(server.convs.get('r1')!.messages.map((m) => m.content)).toEqual([
      'q1',
      'regenerated',
    ]);
    expect(server.calls.filter((c) => c.startsWith('remove:'))).toHaveLength(0);
  });

  it('refuses to truncate when the thread changed elsewhere, keeping local intact', async () => {
    const server = makeServer();
    await server.api.create('r2', 'chat');
    server.convs.get('r2')!.messages = [
      { role: 'user', content: 'q1', meta: null },
      { role: 'assistant', content: 'a1', meta: null },
    ];
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    await store.refresh();
    await store.load('r2');

    // Another tab appends while this one is looking at 2 messages.
    server.convs.get('r2')!.messages.push({
      role: 'user',
      content: 'from another tab',
      meta: null,
    });

    await expect(store.truncateMessages('r2', 1)).rejects.toBeTruthy();
    // Nothing was deleted anywhere.
    expect(server.convs.get('r2')!.messages).toHaveLength(3);
    expect(store.get('r2')!.messages).toHaveLength(2);
  });

  it('saveMessages alone can NEVER shrink — truncate is the only door', async () => {
    const server = makeServer();
    await server.api.create('r3', 'chat');
    server.convs.get('r3')!.messages = [
      { role: 'user', content: 'q1', meta: null },
      { role: 'assistant', content: 'a1', meta: null },
      { role: 'user', content: 'q2', meta: null },
      { role: 'assistant', content: 'a2', meta: null },
    ];
    const store = createServerHistoryStore({
      storage: makeStorage(),
      api: server.api,
    });
    await store.refresh();
    await store.load('r3');

    // Simulate a regenerate that FORGOT to call truncate first.
    store.saveMessages('r3', [msg('user', 'q1'), msg('assistant', 'sneaky')]);
    await store.flush();

    // The server thread is untouched — the shrink did not get through.
    expect(server.convs.get('r3')!.messages.map((m) => m.content)).toEqual([
      'q1',
      'a1',
      'q2',
      'a2',
    ]);
  });
});
