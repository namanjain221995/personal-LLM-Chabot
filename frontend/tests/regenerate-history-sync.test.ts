/**
 * The history sync after a refused whole-thread PUT — 2026-09-13.
 *
 * Two things the recovery used to get wrong, both on the "Try again" path:
 *
 * 1. An answer the server stored itself dedupes against the tab's copy by
 *    generation_id, so the tab's copy was skipped — and with it the only
 *    record of where the answer belongs in the tree, whenever the server's
 *    row had none. A branch-less row attaches to whatever precedes it: a
 *    stacked copy instead of a version.
 * 2. The server copy was adopted into the CACHE only. The view kept showing
 *    the tab's own copy until the next 8-second poll.
 */

import { describe, expect, it } from 'vitest';
import { createServerHistoryStore } from '../lib/history';
import { HistoryApiError, type ServerMessage } from '../lib/historyApi';
import type { ChatMessage } from '../lib/types';

function memoryStorage() {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
  };
}

describe('a whole-thread PUT refused because the server stored an answer', () => {
  it('adopts the server rows, restores the local branch, re-PUTs once and tells the view', async () => {
    const question: ServerMessage = {
      role: 'user',
      content: 'Tell me about marigolds.',
      meta: { branch: { self: 'b-q' }, intent: { id: 'i2', state: 'accepted' } },
    };
    let rows: ServerMessage[] = [question];
    let updatedAt = '2026-09-13T10:00:00Z';
    const puts: { messages: ServerMessage[]; expected?: string }[] = [];

    const api = {
      list: async () => [],
      get: async () => ({ id: 'c1', title: 'Chat', messages: rows, updatedAt }),
      create: async () => undefined,
      update: async () => undefined,
      remove: async () => undefined,
      appendMessage: async () => ({ id: 1 }),
      setFeedback: async () => undefined,
      generateTitle: async () => ({ title: '', generated: false }),
      truncateMessages: async () => {
        throw new Error('the recovery must never truncate');
      },
      replaceMessages: async (_id: string, messages: ServerMessage[], expected?: string) => {
        puts.push({ messages, expected });
        if (expected !== undefined && expected !== updatedAt) {
          throw new HistoryApiError(409, 'conflict', {
            detail: 'conversation changed',
            updated_at: updatedAt,
            messages: rows.length,
          });
        }
        rows = messages;
        updatedAt = '2026-09-13T10:00:09Z';
      },
    };
    const store = createServerHistoryStore({ storage: memoryStorage(), api });
    await store.ready();
    await store.load('c1', { force: true });
    const heard: string[] = [];
    const unsubscribe = store.subscribe?.((id) => heard.push(id));
    expect(unsubscribe).toBeTypeOf('function');

    // Meanwhile the server stored TWO answers — the earlier version and this
    // tab's regenerate — neither carrying a branch (an orchestrator that
    // predates answer_branch), and moved updated_at.
    rows = [
      question,
      { role: 'assistant', content: 'Marigolds are hardy.', meta: { generation_id: 'g1' } },
      { role: 'assistant', content: 'Marigolds repel pests.', meta: { generation_id: 'g2' } },
    ];
    updatedAt = '2026-09-13T10:00:05Z';

    // The tab finalizes what it streamed: the question and ITS answer, which
    // it filed as a version of the question.
    const loaded = store.get('c1')?.messages ?? [];
    store.saveMessages('c1', [
      { ...loaded[0], meta: { ...loaded[0].meta, intent: { id: 'i2', state: 'completed' } } },
      {
        id: 'local-a2',
        role: 'assistant',
        content: 'Marigolds repel pests.',
        createdAt: 2,
        status: 'done',
        meta: { generation_id: 'g2', branch: { self: 'b-i2', parent: 'b-q' } },
      } as ChatMessage,
    ]);
    await store.flush();

    // One refused PUT, then exactly one re-PUT of the repaired server copy.
    expect(puts).toHaveLength(2);
    expect(puts[0].expected).toBe('2026-09-13T10:00:00Z');
    expect(puts[1].expected).toBe('2026-09-13T10:00:05Z');
    expect(puts[1].messages.map((m) => m.content)).toEqual([
      'Tell me about marigolds.',
      'Marigolds are hardy.',
      'Marigolds repel pests.',
    ]);
    expect(puts[1].messages[2].meta?.branch).toEqual({ self: 'b-i2', parent: 'b-q' });
    expect(puts[1].messages[1].meta?.branch).toBeUndefined();

    // The cache ends as [u, a1, a2{branch}] — not as the tab's two rows, and
    // not with the branch lost.
    const cached = store.get('c1')?.messages ?? [];
    expect(cached.map((m) => m.content)).toEqual([
      'Tell me about marigolds.',
      'Marigolds are hardy.',
      'Marigolds repel pests.',
    ]);
    expect(cached[2].meta?.branch).toEqual({ self: 'b-i2', parent: 'b-q' });

    // And the view was told, without waiting for a poll.
    expect(heard).toContain('c1');
    unsubscribe?.();
  });

  it('tells the view even when there is nothing of its own to put back', async () => {
    let rows: ServerMessage[] = [{ role: 'user', content: 'q', meta: {} }];
    let updatedAt = 'v1';
    const puts: number[] = [];
    const api = {
      list: async () => [],
      get: async () => ({ id: 'c1', title: 'Chat', messages: rows, updatedAt }),
      create: async () => undefined,
      update: async () => undefined,
      remove: async () => undefined,
      appendMessage: async () => ({ id: 1 }),
      setFeedback: async () => undefined,
      generateTitle: async () => ({ title: '', generated: false }),
      truncateMessages: async () => undefined,
      replaceMessages: async (_id: string, _m: ServerMessage[], expected?: string) => {
        puts.push(1);
        if (expected !== updatedAt) {
          throw new HistoryApiError(409, 'conflict', {
            detail: 'conversation changed',
            updated_at: updatedAt,
            messages: rows.length,
          });
        }
      },
    };
    const store = createServerHistoryStore({ storage: memoryStorage(), api });
    await store.ready();
    await store.load('c1', { force: true });
    const heard: string[] = [];
    store.subscribe?.((id) => heard.push(id));

    rows = [
      ...rows,
      {
        role: 'assistant',
        content: 'a',
        meta: { generation_id: 'g1', branch: { self: 'b-i1', parent: '#0' } },
      },
    ];
    updatedAt = 'v2';
    const loaded = store.get('c1')?.messages ?? [];
    store.saveMessages('c1', [
      { ...loaded[0], meta: { intent: { id: 'i1', state: 'completed' } } },
      {
        id: 'l',
        role: 'assistant',
        content: 'a',
        createdAt: 1,
        meta: { generation_id: 'g1', branch: { self: 'b-i1', parent: '#0' } },
      } as ChatMessage,
    ]);
    await store.flush();

    expect(puts).toHaveLength(1);
    expect(heard).toEqual(['c1']);
    expect(store.get('c1')?.messages.map((m) => m.content)).toEqual(['q', 'a']);
  });
});
