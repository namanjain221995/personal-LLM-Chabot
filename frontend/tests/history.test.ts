import { describe, expect, it } from 'vitest';
import {
  createHistoryStore,
  titleFromFirstMessage,
  type StorageLike,
} from '../lib/history';
import type { ChatMessage, ConversationSummary } from '../lib/types';

/** In-memory StorageLike with an optional byte budget (quota simulation). */
function makeStorage(maxBytes = Infinity): StorageLike & {
  bytes: () => number;
} {
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
    removeItem: (k) => {
      map.delete(k);
    },
    bytes: () => [...map.values()].reduce((n, v) => n + v.length, 0),
  };
}

function msg(role: 'user' | 'assistant', content: string): ChatMessage {
  return {
    id: `${role}-${Math.random().toString(36).slice(2)}`,
    role,
    content,
    createdAt: Date.now(),
  };
}

describe('titleFromFirstMessage', () => {
  it('uses the first message, truncated at 40 chars', () => {
    expect(titleFromFirstMessage('win rate this fiscal year vs last')).toBe(
      'win rate this fiscal year vs last',
    );
    const long =
      'top 10 accounts by open opportunity value across every region we sell in';
    const title = titleFromFirstMessage(long);
    expect(title.length).toBeLessThanOrEqual(40);
    expect(title.endsWith('…')).toBe(true);
  });

  it('collapses whitespace and falls back for empty input', () => {
    expect(titleFromFirstMessage('  a\n b ')).toBe('a b');
    expect(titleFromFirstMessage('   ')).toBe('New chat');
  });
});

describe('history store CRUD', () => {
  it('creates conversations and lists them newest-first', () => {
    const store = createHistoryStore(makeStorage());
    const a = store.create('first question');
    const b = store.create('second question');
    store.saveMessages(b.id, [msg('user', 'second question')]);
    const list = store.list();
    expect(list).toHaveLength(2);
    expect(list[0].id).toBe(b.id);
    expect(list.map((c) => c.title)).toContain('first question');
    expect(store.get(a.id)?.title).toBe('first question');
  });

  it('persists messages including meta', () => {
    const store = createHistoryStore(makeStorage());
    const conv = store.create('chart cases');
    const assistant: ChatMessage = {
      ...msg('assistant', 'Here is the chart.'),
      status: 'done',
      meta: {
        route: 'sql',
        sql: 'SELECT 1',
        data: [{ n: 1 }],
        truncated: false,
      },
    };
    store.saveMessages(conv.id, [msg('user', 'chart cases'), assistant]);
    const loaded = store.get(conv.id);
    expect(loaded?.messages).toHaveLength(2);
    expect(loaded?.messages[1].meta?.route).toBe('sql');
    expect(loaded?.messages[1].meta?.sql).toBe('SELECT 1');
  });

  it('renames (trimmed, capped) and ignores blank titles', () => {
    const store = createHistoryStore(makeStorage());
    const conv = store.create('original');
    store.rename(conv.id, '  Pipeline deep dive  ');
    expect(store.get(conv.id)?.title).toBe('Pipeline deep dive');
    store.rename(conv.id, '   ');
    expect(store.get(conv.id)?.title).toBe('Pipeline deep dive');
  });

  it('deletes conversations', () => {
    const store = createHistoryStore(makeStorage());
    const a = store.create('keep me');
    const b = store.create('delete me');
    store.remove(b.id);
    expect(store.list().map((c) => c.id)).toEqual([a.id]);
    expect(store.get(b.id)).toBeNull();
  });
});

describe('history store pin + archive (V3 §2)', () => {
  /** Seeds the cache with explicit timestamps so ordering is unambiguous. */
  function seeded() {
    const storage = makeStorage();
    storage.setItem(
      'techsara.history.v1',
      JSON.stringify([
        { id: 'a', title: 'older pin', createdAt: 1, updatedAt: 10, pinned: true, messages: [] },
        { id: 'b', title: 'newer pin', createdAt: 1, updatedAt: 30, pinned: true, messages: [] },
        { id: 'c', title: 'newest', createdAt: 1, updatedAt: 40, messages: [] },
        { id: 'd', title: 'older', createdAt: 1, updatedAt: 20, messages: [] },
        { id: 'e', title: 'filed', createdAt: 1, updatedAt: 50, archived: true, messages: [] },
      ]),
    );
    return createHistoryStore(storage);
  }

  it('lists pinned first, then newest, and hides archived chats', () => {
    const store = seeded();
    expect(store.list().map((c) => c.id)).toEqual(['b', 'a', 'c', 'd']);
    expect(store.list().map((c) => c.pinned)).toEqual([true, true, false, false]);
    expect(store.listArchived().map((c) => c.id)).toEqual(['e']);
  });

  it('flips the flags without disturbing recency', () => {
    const store = seeded();
    store.setPinned('c', true);
    expect(store.list().map((c) => c.id)).toEqual(['c', 'b', 'a', 'd']);
    expect(store.get('c')?.updatedAt).toBe(40);

    store.setArchived('c', true);
    expect(store.list().map((c) => c.id)).toEqual(['b', 'a', 'd']);
    // 'c' is still pinned, and pinned-first applies inside Archived too.
    expect(store.listArchived().map((c) => c.id)).toEqual(['c', 'e']);
    expect(store.get('c')?.updatedAt).toBe(40);

    store.setArchived('e', false);
    expect(store.list().map((c) => c.id)).toEqual(['b', 'a', 'e', 'd']);
  });
});

describe('history store quota handling', () => {
  it('drops the oldest conversation on QuotaExceeded and reports it', () => {
    const evicted: ConversationSummary[] = [];
    const storage = makeStorage(4000);
    const store = createHistoryStore(storage, (d) => evicted.push(d));

    const oldest = store.create('oldest conversation');
    store.saveMessages(oldest.id, [msg('user', 'x'.repeat(1500))]);
    const middle = store.create('middle conversation');
    store.saveMessages(middle.id, [msg('user', 'y'.repeat(1200))]);

    // This write cannot fit alongside both others: the oldest must go.
    const newest = store.create('newest conversation');
    store.saveMessages(newest.id, [msg('user', 'z'.repeat(1200))]);

    expect(evicted.length).toBeGreaterThanOrEqual(1);
    expect(evicted[0].id).toBe(oldest.id);
    const ids = store.list().map((c) => c.id);
    expect(ids).not.toContain(oldest.id);
    expect(ids).toContain(newest.id);
  });

  it('drops repeatedly until the payload fits', () => {
    const evicted: ConversationSummary[] = [];
    const storage = makeStorage(2600);
    const store = createHistoryStore(storage, (d) => evicted.push(d));

    const a = store.create('a');
    store.saveMessages(a.id, [msg('user', 'a'.repeat(900))]);
    const b = store.create('b');
    store.saveMessages(b.id, [msg('user', 'b'.repeat(900))]);
    const c = store.create('c');
    store.saveMessages(c.id, [msg('user', 'c'.repeat(1800))]);

    expect(evicted.map((d) => d.title)).toEqual(['a', 'b']);
    expect(store.list().map((d) => d.title)).toEqual(['c']);
  });

  it('rethrows non-quota storage errors', () => {
    const broken: StorageLike = {
      getItem: () => null,
      setItem: () => {
        throw new Error('disk on fire');
      },
      removeItem: () => undefined,
    };
    const store = createHistoryStore(broken);
    expect(() => store.create('boom')).toThrow('disk on fire');
  });
});
