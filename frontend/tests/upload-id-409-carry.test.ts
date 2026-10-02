/**
 * RC-3c, the sequence the real two-browser run lost 3 of 3 times
 * (docs/chat-media/NOTES.md, e2e 2026-10-02).
 *
 * Phone A sends a photo and a PDF in one turn. The turn is APPENDED (POST
 * /messages), which moves the server's `updated_at` past the stamp A holds.
 * The PDF's background upload lands and writes `meta.attachments[i].id` onto
 * the turn; the whole-thread PUT carrying it quotes the old stamp and is
 * refused (409 conversation changed). The recovery adopted the server's copy
 * of the turn (no id, `upload_state: 'selected'`), found nothing of its own
 * to re-apply, and never pushed again. No other device could open the PDF.
 *
 * The fake below keeps the server's real V29 rule rather than scripting the
 * refusals: every write moves `updated_at`, and a PUT whose
 * `expected_updated_at` is not the current value is refused.
 */

import { describe, expect, it } from 'vitest';
import { withAttachmentPatched } from '../lib/attachments';
import { createServerHistoryStore, type StorageLike } from '../lib/history';
import {
  HistoryApiError,
  type HistoryApi,
  type ServerMessage,
} from '../lib/historyApi';
import { reconcileThread, withStoredUploadIds } from '../lib/threadReconcile';
import type { ChatMessage, MessageAttachment } from '../lib/types';

function makeStorage(): StorageLike {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

/** A real request serializes: the fake never shares objects with the store. */
const wire = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

interface Put {
  messages: ServerMessage[];
  accepted: boolean;
}

function makeServer() {
  const convs = new Map<
    string,
    { title: string; messages: ServerMessage[]; version: number }
  >();
  const puts: Put[] = [];
  let nextRowId = 1;
  /** Run after the next GET has answered: the server moves meanwhile. */
  const afterGet: Array<(id: string) => void> = [];
  const stamp = (id: string) => `v${convs.get(id)!.version}`;
  const touch = (id: string) => {
    convs.get(id)!.version += 1;
  };
  const api: HistoryApi = {
    async list() {
      return [];
    },
    async get(id) {
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      const out = {
        id,
        title: c.title,
        messages: wire(c.messages),
        updatedAt: stamp(id),
      };
      afterGet.shift()?.(id);
      return out;
    },
    async create(id, title) {
      if (id !== undefined && convs.has(id)) {
        throw new HistoryApiError(409, 'conversation id already exists');
      }
      convs.set(id ?? `gen-${convs.size}`, { title, messages: [], version: 1 });
    },
    async update() {},
    async remove(id) {
      convs.delete(id);
    },
    async appendMessage(id, message) {
      const c = convs.get(id)!;
      const row = { ...wire(message), id: nextRowId++ };
      c.messages.push(row);
      touch(id);
      return { id: row.id };
    },
    async truncateMessages() {},
    async generateTitle() {
      return { title: '', generated: false };
    },
    async setFeedback() {},
    async replaceMessages(id, messages, expectedUpdatedAt) {
      const c = convs.get(id)!;
      if (expectedUpdatedAt !== undefined && expectedUpdatedAt !== stamp(id)) {
        puts.push({ messages: wire(messages), accepted: false });
        throw new HistoryApiError(409, 'conversation changed', {
          detail: 'conversation changed',
          updated_at: stamp(id),
          messages: c.messages.length,
        });
      }
      if (messages.length < c.messages.length) {
        puts.push({ messages: wire(messages), accepted: false });
        throw new HistoryApiError(409, 'refusing to shrink conversation');
      }
      puts.push({ messages: wire(messages), accepted: true });
      c.messages = messages.map((m) => ({ ...wire(m), id: nextRowId++ }));
      touch(id);
    },
  };
  return {
    api,
    convs,
    puts,
    /** The orchestrator stores the finished answer itself (RC-4). */
    storesAnswerAfterNextGet() {
      afterGet.push((id) => {
        convs.get(id)!.messages.push({
          role: 'assistant',
          content: 'The report says revenue grew.',
          meta: { generation_id: 'gen-2' },
          id: nextRowId++,
        });
        touch(id);
      });
    },
    /** Something else writes the thread after every read, for good. */
    keepsMoving() {
      for (let i = 0; i < 50; i += 1) afterGet.push((id) => touch(id));
    },
  };
}

const DOC = 'att-doc-0001';
const UPLOAD = 'up-7f3a9c';

function turn(state: MessageAttachment['upload_state'] = 'selected'): ChatMessage {
  return {
    id: 'u-2',
    role: 'user',
    content: 'what does the chart and the report say?',
    createdAt: 3,
    meta: {
      images: [{ attachment_id: 'att-img-0001', mime: 'image/jpeg', width: 1200, height: 900 }],
      attachments: [{ attachment_id: DOC, name: 'report.pdf', kind: 'pdf', upload_state: state }],
    },
  };
}

const firstTurn: ChatMessage[] = [
  { id: 'u-1', role: 'user', content: 'hello', createdAt: 1 },
  { id: 'a-1', role: 'assistant', content: 'Hi.', status: 'done', createdAt: 2 },
];

const answer: ChatMessage = {
  id: 'a-2',
  role: 'assistant',
  content: 'The report says revenue grew.',
  status: 'done',
  createdAt: 4,
  meta: { generation_id: 'gen-2' },
};

/** The stored turn's document entry, as the server holds it. */
const storedDoc = (server: ReturnType<typeof makeServer>, id: string) =>
  server.convs.get(id)!.messages[2]?.meta?.attachments?.[0];

const hasUploadId = (messages: ServerMessage[] | ChatMessage[]) =>
  messages[2]?.meta?.attachments?.[0]?.id === UPLOAD;

/**
 * A has the chat open (so it holds a stamp), appends the photo + PDF turn,
 * and then the PDF's background upload lands — the write-back patches the
 * LATEST stored thread by the file's own attachment_id (ChatApp's inline
 * path).
 */
async function sendThenUploadLands(server: ReturnType<typeof makeServer>) {
  const s = createServerHistoryStore({ storage: makeStorage(), api: server.api });
  const conv = s.create('hello');
  s.saveMessages(conv.id, firstTurn);
  await s.flush();
  await s.load(conv.id, { force: true }); // A opens the chat: stamp learned
  s.saveMessages(conv.id, [...s.get(conv.id)!.messages, turn()]);
  await s.flush(); // POST /messages: the server moved past A's stamp
  const putsBefore = server.puts.length;
  const stored = s.get(conv.id)!.messages;
  const patched = withAttachmentPatched(stored, DOC, {
    id: UPLOAD,
    upload_state: 'uploaded',
  });
  expect(patched).not.toBe(stored);
  return { s, id: conv.id, patched, putsBefore };
}

describe('RC-3c: the upload id survives a refused whole-thread PUT', () => {
  it('one refusal: the recovery re-pushes once, and that push carries the id', async () => {
    const server = makeServer();
    const { s, id, patched, putsBefore } = await sendThenUploadLands(server);

    s.saveMessages(id, patched);
    await s.flush();

    const puts = server.puts.slice(putsBefore);
    expect(puts.map((p) => p.accepted)).toEqual([false, true]);
    expect(hasUploadId(puts[0].messages)).toBe(true); // refused: stale stamp
    expect(hasUploadId(puts[1].messages)).toBe(true); // the re-push
    expect(storedDoc(server, id)).toMatchObject({ id: UPLOAD, upload_state: 'uploaded' });
    expect(s.get(id)!.messages[2].meta?.attachments?.[0]).toMatchObject({
      id: UPLOAD,
      upload_state: 'uploaded',
    });
    // The photo reference rode along untouched.
    expect(server.convs.get(id)!.messages[2].meta?.images?.[0].attachment_id).toBe(
      'att-img-0001',
    );
  });

  it('two refusals in a row: the cache keeps the id and the next push carries it', async () => {
    const server = makeServer();
    const { s, id, patched, putsBefore } = await sendThenUploadLands(server);

    // Between the recovery's read and its re-push, the server stores the
    // finished answer itself: the re-push is refused too.
    server.storesAnswerAfterNextGet();
    s.saveMessages(id, patched);
    await s.flush();

    const round = server.puts.slice(putsBefore);
    expect(round.map((p) => p.accepted)).toEqual([false, false]);
    expect(round.every((p) => hasUploadId(p.messages))).toBe(true);
    // Nothing stored the id yet, but this browser has not forgotten it.
    expect(storedDoc(server, id)?.id).toBeUndefined();
    expect(hasUploadId(s.get(id)!.messages)).toBe(true);
    expect(s.get(id)!.messages).toHaveLength(4); // the server's answer adopted

    // The next push (the dirty retry on the next refresh) carries it.
    await s.refresh();
    await s.flush();
    const retry = server.puts.slice(putsBefore + 2);
    expect(retry.map((p) => p.accepted)).toEqual([true]);
    expect(hasUploadId(retry[0].messages)).toBe(true);
    expect(storedDoc(server, id)).toMatchObject({ id: UPLOAD, upload_state: 'uploaded' });
    expect(server.convs.get(id)!.messages).toHaveLength(4);
  });

  it('the stream saving its turn list captured at send does not undo it', async () => {
    const server = makeServer();
    const { s, id, patched, putsBefore } = await sendThenUploadLands(server);
    server.storesAnswerAfterNextGet();
    s.saveMessages(id, patched);
    await s.flush();

    // The stream ends and saves the thread it captured at send — the turn's
    // entry without the id — plus the answer the server also stored.
    s.saveMessages(id, [...firstTurn, turn('selected'), answer]);
    await s.flush();

    expect(storedDoc(server, id)).toMatchObject({ id: UPLOAD, upload_state: 'uploaded' });
    expect(server.convs.get(id)!.messages).toHaveLength(4); // no duplicate answer
    expect(hasUploadId(s.get(id)!.messages)).toBe(true);
    // Once a push has carried the id, no later push leaves it out.
    const later = server.puts.slice(putsBefore);
    const first = later.findIndex((p) => hasUploadId(p.messages));
    expect(first).toBe(0);
    expect(later.every((p) => hasUploadId(p.messages))).toBe(true);
  });
});

describe('RC-3c: no push storm', () => {
  it('a settled thread costs no further write', async () => {
    const server = makeServer();
    const { s, id, patched } = await sendThenUploadLands(server);
    s.saveMessages(id, patched);
    await s.flush();
    const settled = server.puts.length;

    for (let i = 0; i < 5; i += 1) {
      s.saveMessages(id, wire(s.get(id)!.messages));
      await s.flush();
    }
    await s.refresh();
    await s.flush();
    expect(server.puts.length).toBe(settled);
  });

  it('a server that moves after every read costs two PUTs per save, not a loop', async () => {
    const server = makeServer();
    const { s, id, patched, putsBefore } = await sendThenUploadLands(server);
    server.keepsMoving();
    s.saveMessages(id, patched);
    await s.flush();
    expect(server.puts.length - putsBefore).toBe(2);
    expect(hasUploadId(s.get(id)!.messages)).toBe(true);
  });
});

describe('RC-3c: an upload id never regresses to absent', () => {
  const withEntry = (entry: MessageAttachment, content = turn().content): ChatMessage => ({
    ...turn(),
    content,
    meta: { ...turn().meta, attachments: [entry] },
  });
  const landed = withEntry({
    attachment_id: DOC,
    name: 'report.pdf',
    kind: 'pdf',
    id: UPLOAD,
    upload_state: 'uploaded',
  });
  const bare = withEntry({ attachment_id: DOC, name: 'report.pdf', kind: 'pdf', upload_state: 'selected' });

  it('a copy without the id takes it from the copy that has it', () => {
    const out = withStoredUploadIds([...firstTurn, bare], [...firstTurn, landed]);
    expect(out[2].meta?.attachments?.[0]).toMatchObject({ id: UPLOAD, upload_state: 'uploaded' });
    // Everything else on the entry and the turn is the newer copy's own.
    expect(out[2].meta?.attachments?.[0].name).toBe('report.pdf');
    expect(out[2].meta?.images).toEqual(turn().meta?.images);
  });

  it('an id is never replaced, and an unrelated turn or file takes nothing', () => {
    const other = withEntry({ attachment_id: DOC, name: 'report.pdf', kind: 'pdf', id: 'up-other' });
    const keep = [...firstTurn, other];
    expect(withStoredUploadIds(keep, [...firstTurn, landed])).toBe(keep);

    const reworded = [...firstTurn, withEntry(bare.meta!.attachments![0], 'a different question')];
    expect(withStoredUploadIds(reworded, [...firstTurn, landed])).toBe(reworded);

    const otherFile = [
      ...firstTurn,
      withEntry({ attachment_id: 'att-doc-0002', name: 'report.pdf', kind: 'pdf' }),
    ];
    expect(withStoredUploadIds(otherFile, [...firstTurn, landed])).toBe(otherFile);
  });

  it('the view folding in a server copy without the id keeps it', () => {
    const merged = reconcileThread([...firstTurn, landed, answer], [...firstTurn, bare, answer]);
    expect(merged[2].meta?.attachments?.[0]).toMatchObject({ id: UPLOAD, upload_state: 'uploaded' });
  });

  it('a save from a stale copy after the id landed keeps it, locally and on the server', async () => {
    const server = makeServer();
    const { s, id, patched } = await sendThenUploadLands(server);
    s.saveMessages(id, patched);
    await s.flush();

    s.saveMessages(id, [...wire(s.get(id)!.messages.slice(0, 2)), turn('selected'), answer]);
    expect(hasUploadId(s.get(id)!.messages)).toBe(true);
    await s.flush();
    expect(storedDoc(server, id)).toMatchObject({ id: UPLOAD, upload_state: 'uploaded' });
  });
});
