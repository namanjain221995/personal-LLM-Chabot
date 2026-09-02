/**
 * PHASE 2 — a change to an ALREADY-PUSHED message must still reach the server.
 *
 * The sync state recorded, per conversation, the list of message IDS the
 * server was known to hold. `syncConversation` then decided it had nothing to
 * do whenever that list was a prefix of the local thread AND the two were the
 * same length — "same ids, same count, therefore same conversation".
 *
 * That is true of identity and false of CONTENT, and the dataset flow is built
 * entirely on the difference. It persists the user turn, uploads the file,
 * and only then learns the `upload_id`, which it writes onto the message it
 * already saved and persists a second time. Ids unchanged, count unchanged —
 * so the second push was skipped and the id never left the browser. Measured
 * on the live database at audit time: 7 of 40 attachment messages carried an
 * `upload_id`, and the 7 were the ones that happened to win a race with the
 * first push.
 *
 * These tests pin the contract that replaces it: the sync state records what
 * was SENT, not merely which messages were sent, so any divergence in the
 * server-visible payload (`role`, `content`, `meta`, `feedback`) is repaired —
 * while an unchanged thread still costs exactly zero writes.
 */

import { describe, expect, it } from 'vitest';
import { createServerHistoryStore, type StorageLike } from '../lib/history';
import {
  HistoryApiError,
  type HistoryApi,
  type ServerMessage,
} from '../lib/historyApi';
import type { ChatMessage, Meta } from '../lib/types';

/* ------------------------------------------------------------- fakes */

function makeStorage(): StorageLike {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

/**
 * A real request SERIALIZES. Without this the fake hands the store back its
 * own objects, so mutating `message.meta` in the browser silently "updates"
 * the server too — and a sync bug about propagating that mutation passes
 * without the sync ever running. (It did: P2-01 and P2-02 both went green
 * against the unfixed store until this clone was added.)
 */
const wire = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/**
 * The half of the history API this suite exercises, with the server's real
 * invariants: a duplicate create is 409, and a replace may never shrink.
 */
function makeServer() {
  const convs = new Map<string, { title: string; messages: ServerMessage[] }>();
  const calls: string[] = [];
  let nextRowId = 1;
  const api: HistoryApi = {
    async list() {
      calls.push('list');
      return [];
    },
    async get(id) {
      calls.push(`get:${id}`);
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      return { id, title: c.title, messages: c.messages };
    },
    async create(id, title) {
      calls.push(`create:${id ?? '?'}`);
      if (id !== undefined && convs.has(id)) {
        throw new HistoryApiError(409, 'conversation id already exists');
      }
      convs.set(id ?? `gen-${convs.size}`, { title, messages: [] });
    },
    async update(id) {
      calls.push(`update:${id}`);
    },
    async remove(id) {
      calls.push(`remove:${id}`);
      convs.delete(id);
    },
    async appendMessage(id, message) {
      calls.push(`append:${id}`);
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      const row = { ...wire(message), id: nextRowId++ };
      c.messages.push(row);
      return { id: row.id };
    },
    async truncateMessages(id, keep, expectedTotal) {
      calls.push(`truncate:${id}`);
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      if (c.messages.length !== expectedTotal) {
        throw new HistoryApiError(409, 'conversation changed');
      }
      c.messages = c.messages.slice(0, keep);
    },
    async generateTitle() {
      return { title: '', generated: false };
    },
    async setFeedback(id, messageId, feedback) {
      calls.push('setFeedback');
      const target = convs.get(id)?.messages.find((m) => m.id === messageId);
      if (!target) throw new HistoryApiError(404, 'message not found');
      target.feedback = feedback;
    },
    async replaceMessages(id, messages) {
      calls.push(`replace:${id}`);
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      if (messages.length < c.messages.length) {
        throw new HistoryApiError(409, 'refusing to shrink conversation');
      }
      c.messages = messages.map((m) => ({ ...wire(m), id: nextRowId++ }));
    },
  };
  return { api, convs, calls };
}

function store(server: ReturnType<typeof makeServer>) {
  return createServerHistoryStore({
    storage: makeStorage(),
    api: server.api,
  });
}

let seq = 0;
function msg(
  role: 'user' | 'assistant',
  content: string,
  meta?: Meta,
): ChatMessage {
  seq += 1;
  return {
    id: `${role}-${seq}`,
    role,
    content,
    createdAt: 1_000 + seq,
    ...(meta ? { meta } : {}),
  };
}

/** The message the dataset flow persists BEFORE its upload resolves. */
function datasetTurn(name: string): ChatMessage {
  return msg('user', '', {
    route: 'chat',
    attachments: [{ name, kind: 'dataset' }],
  });
}

const writesTo = (server: ReturnType<typeof makeServer>, id: string) =>
  server.calls.filter(
    (c) => c === `append:${id}` || c === `replace:${id}`,
  ).length;

/* -------------------------------------------------------------- tests */

describe('P2 · a payload change on an already-pushed message syncs', () => {
  it('P2-01 — same ids, same count, upload_id added → the server is updated', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('dataset chat');
    const turn = datasetTurn('report.xlsx');

    // 1. The turn is persisted the moment it goes on screen — no upload_id yet.
    s.saveMessages(conv.id, [turn]);
    await s.flush();
    expect(
      server.convs.get(conv.id)?.messages[0].meta?.attachments?.[0].id,
    ).toBeUndefined();

    // 2. POST /api/upload resolves. ChatApp writes the id onto the message it
    //    already saved (same object, same id, same count) and persists again.
    turn.meta!.attachments![0].id = 'abc123';
    s.saveMessages(conv.id, [turn]);
    await s.flush();

    expect(
      server.convs.get(conv.id)?.messages[0].meta?.attachments?.[0],
    ).toEqual({ id: 'abc123', name: 'report.xlsx', kind: 'dataset' });
  });

  it('P2-02 — the upload_id survives a reload from server history', async () => {
    const server = makeServer();
    const first = store(server);
    const conv = first.create('dataset chat');
    const turn = datasetTurn('report.xlsx');
    first.saveMessages(conv.id, [turn]);
    await first.flush();
    turn.meta!.attachments![0].id = 'abc123';
    first.saveMessages(conv.id, [turn]);
    await first.flush();

    // A different browser/tab: empty cache, everything comes from the server.
    const second = store(server);
    await second.refresh();
    const loaded = await second.load(conv.id);

    expect(loaded?.messages[0].meta?.attachments?.[0].id).toBe('abc123');
    // The file card is still rebuilt from the same metadata (2026-08-21).
    expect(loaded?.messages[0].pdfName).toBe('report.xlsx');
  });

  it('P2-03 — any meta change on a pushed message syncs, not just upload_id', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('c');
    const a = msg('assistant', 'the answer', { route: 'chat' });
    s.saveMessages(conv.id, [a]);
    await s.flush();

    a.meta = { route: 'report', generation_id: 'g-9' };
    s.saveMessages(conv.id, [a]);
    await s.flush();

    expect(server.convs.get(conv.id)?.messages[0].meta).toEqual({
      route: 'report',
      generation_id: 'g-9',
    });
  });

  it('P2-03b — a content change on a pushed message syncs too', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('c');
    const a = msg('assistant', 'partial');
    s.saveMessages(conv.id, [a]);
    await s.flush();

    a.content = 'the complete answer';
    s.saveMessages(conv.id, [a]);
    await s.flush();

    expect(server.convs.get(conv.id)?.messages[0].content).toBe(
      'the complete answer',
    );
  });

  it('P2-04 — an identical thread costs NO write at all', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('c');
    const turn = msg('user', 'hello');
    s.saveMessages(conv.id, [turn]);
    await s.flush();
    const after = writesTo(server, conv.id);

    // Saving the same state again, repeatedly, must stay silent — this is the
    // guard the old length check was really providing, and it has to survive.
    s.saveMessages(conv.id, [turn]);
    await s.flush();
    s.saveMessages(conv.id, [{ ...turn }]);
    await s.flush();

    expect(writesTo(server, conv.id)).toBe(after);
  });

  it('P2-04b — a browser-only field change is not a server change', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('c');
    const turn = msg('user', 'hello');
    s.saveMessages(conv.id, [turn]);
    await s.flush();
    const after = writesTo(server, conv.id);

    // `imageDataUrl` and `serverId` never reach the server (toServerMessage
    // omits them), so touching them must not provoke a push.
    s.saveMessages(conv.id, [
      { ...turn, imageDataUrl: 'data:image/png;base64,AAA', serverId: 7 },
    ]);
    await s.flush();

    expect(writesTo(server, conv.id)).toBe(after);
  });
});

describe('P2 · nothing else about syncing changes', () => {
  it('P2-05 — a pure append still appends, and does not rewrite the thread', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('c');
    const u = msg('user', 'question');
    s.saveMessages(conv.id, [u]);
    await s.flush();
    server.calls.length = 0;

    const a = msg('assistant', 'answer');
    s.saveMessages(conv.id, [u, a]);
    await s.flush();

    expect(server.calls).toContain(`append:${conv.id}`);
    expect(server.calls).not.toContain(`replace:${conv.id}`);
    expect(server.convs.get(conv.id)?.messages.map((m) => m.content)).toEqual([
      'question',
      'answer',
    ]);
  });

  it('P2-05b — an append still learns the server row id (thumbs depend on it)', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('c');
    const u = msg('user', 'question');
    s.saveMessages(conv.id, [u]);
    await s.flush();
    // The SECOND save is the append — the only path that reports row ids back.
    const a = msg('assistant', 'answer');
    s.saveMessages(conv.id, [u, a]);
    await s.flush();
    const stored = s.get(conv.id)?.messages ?? [];
    expect(typeof stored[1]?.serverId).toBe('number');
  });

  it('P2-06 — truncation is unchanged and does not resurrect the dropped tail', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('c');
    const u = msg('user', 'q');
    const a = msg('assistant', 'a');
    s.saveMessages(conv.id, [u, a]);
    await s.flush();

    await s.truncateMessages(conv.id, 1);
    await s.flush();

    expect(server.convs.get(conv.id)?.messages.map((m) => m.content)).toEqual([
      'q',
    ]);
    // And the store now considers itself in sync — no follow-up write.
    server.calls.length = 0;
    s.saveMessages(conv.id, [u]);
    await s.flush();
    expect(writesTo(server, conv.id)).toBe(0);
  });

  it('P2-07 — branch metadata survives a payload repair', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('c');
    const u = msg('user', 'q', { branch: { self: 'b1' } });
    const a = msg('assistant', 'a', { branch: { self: 'b2', parent: 'b1' } });
    s.saveMessages(conv.id, [u, a]);
    await s.flush();

    // A meta change on the FIRST message forces a whole-thread repair; the
    // second message's branch pointers must come through untouched.
    u.meta = { ...u.meta, attachments: [{ id: 'up-1', name: 'd.csv', kind: 'dataset' }] };
    s.saveMessages(conv.id, [u, a]);
    await s.flush();

    const rows = server.convs.get(conv.id)!.messages;
    expect(rows[0].meta?.branch).toEqual({ self: 'b1' });
    expect(rows[1].meta?.branch).toEqual({ self: 'b2', parent: 'b1' });
    expect(rows[0].meta?.attachments?.[0].id).toBe('up-1');
  });

  it('P2-07b — a repair never shrinks or reorders the thread', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('c');
    const u = msg('user', 'q');
    const a = msg('assistant', 'a');
    s.saveMessages(conv.id, [u, a]);
    await s.flush();

    u.meta = { route: 'chat' };
    s.saveMessages(conv.id, [u, a]);
    await s.flush();

    expect(server.convs.get(conv.id)?.messages.map((m) => m.role)).toEqual([
      'user',
      'assistant',
    ]);
  });

  it('feedback already on a message is not lost by a payload repair', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('c');
    const u = msg('user', 'q');
    const a: ChatMessage = { ...msg('assistant', 'a'), feedback: 'up' };
    s.saveMessages(conv.id, [u, a]);
    await s.flush();

    u.meta = { route: 'chat' };
    s.saveMessages(conv.id, [u, a]);
    await s.flush();

    expect(server.convs.get(conv.id)?.messages[1].feedback).toBe('up');
  });
});
