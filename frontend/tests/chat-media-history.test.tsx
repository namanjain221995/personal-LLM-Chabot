// @vitest-environment jsdom
/**
 * `meta.images` through history (2026-10-02, docs/chat-media/CONTRACT.md §7).
 *
 * The reference is written by the BROWSER at send and rides the first push;
 * history stores `meta` verbatim, last writer wins. So it has to be:
 *
 *   · STABLE — the sync key hashes all of meta, and a field that changed on
 *     its own would re-push the whole thread on every save (a push storm);
 *   · DURABLE — no older copy of the thread (the view a render behind, a
 *     stream that captured it at send, a server copy adopted after a 409)
 *     may erase it, or the photo disappears from every other device;
 *   · ENOUGH — a hydrate on a fresh store, the second device, renders the
 *     stored thumbnails from it and nothing else.
 *
 * P2-04b stays true alongside: `imageDataUrl` is browser-only and never
 * provokes or rides a push.
 */

import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { MessageRow } from '@/components/MessageRow';
import { createServerHistoryStore, type StorageLike } from '@/lib/history';
import { HistoryApiError, type HistoryApi, type ServerMessage } from '@/lib/historyApi';
import type { ChatMessage, MessageImage } from '@/lib/types';

afterEach(() => cleanup());

function makeStorage(): StorageLike {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
}

/** A real request serializes: the fake must never share objects with the store. */
const wire = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

function makeServer() {
  const convs = new Map<string, { title: string; messages: ServerMessage[] }>();
  const calls: string[] = [];
  const bodies: ServerMessage[][] = [];
  /** Refuse the next N whole-thread PUTs as "conversation changed". */
  let moved = 0;
  let nextRowId = 1;
  const api: HistoryApi = {
    async list() {
      return [];
    },
    async get(id) {
      calls.push(`get:${id}`);
      const c = convs.get(id);
      if (!c) throw new HistoryApiError(404, 'not found');
      return { id, title: c.title, messages: wire(c.messages), updatedAt: 't1' };
    },
    async create(id, title) {
      if (id !== undefined && convs.has(id)) throw new HistoryApiError(409, 'exists');
      convs.set(id ?? `gen-${convs.size}`, { title, messages: [] });
    },
    async update() {},
    async remove(id) {
      convs.delete(id);
    },
    async appendMessage(id, message) {
      calls.push(`append:${id}`);
      bodies.push([wire(message)]);
      const c = convs.get(id)!;
      const row = { ...wire(message), id: nextRowId++ };
      c.messages.push(row);
      return { id: row.id };
    },
    async truncateMessages() {},
    async generateTitle() {
      return { title: '', generated: false };
    },
    async setFeedback() {},
    async replaceMessages(id, messages) {
      calls.push(`replace:${id}`);
      bodies.push(wire(messages));
      const c = convs.get(id)!;
      if (moved > 0) {
        moved -= 1;
        throw new HistoryApiError(409, 'conversation changed', {
          detail: 'conversation changed',
          updated_at: 't2',
        });
      }
      if (messages.length < c.messages.length) throw new HistoryApiError(409, 'shrink');
      c.messages = messages.map((m) => ({ ...wire(m), id: nextRowId++ }));
    },
  };
  return {
    api,
    convs,
    calls,
    bodies,
    refuseNextPut: (n = 1) => {
      moved = n;
    },
  };
}

const store = (server: ReturnType<typeof makeServer>) =>
  createServerHistoryStore({ storage: makeStorage(), api: server.api });

const writes = (server: ReturnType<typeof makeServer>, id: string) =>
  server.calls.filter((c) => c === `append:${id}` || c === `replace:${id}`).length;

const IMAGES: MessageImage[] = [
  { attachment_id: 'img-aaaa-0001', name: 'cat.jpg', mime: 'image/jpeg', width: 1600, height: 1200 },
  { attachment_id: 'img-aaaa-0002', name: 'dog.png', mime: 'image/png', width: 800, height: 800 },
];
const PNG = 'data:image/png;base64,iVBORw0KGgo=';

function photoTurn(id = 'u-1'): ChatMessage {
  return {
    id,
    role: 'user',
    content: 'are these healthy?',
    imageDataUrl: PNG,
    imageDataUrls: [PNG, PNG],
    meta: { images: IMAGES.map((i) => ({ ...i })) },
    createdAt: 1,
  };
}
const answer = (content = 'Both look healthy.'): ChatMessage => ({
  id: 'a-1',
  role: 'assistant',
  content,
  meta: { route: 'vision' },
  status: 'done',
  createdAt: 2,
});

describe('meta.images rides the first push, and never pushes again by itself', () => {
  it('travels with the turn, without a single data URL', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('photos');
    s.saveMessages(conv.id, [photoTurn()]);
    await s.flush();

    const row = server.convs.get(conv.id)!.messages[0];
    expect(row.meta?.images).toEqual(IMAGES);
    // P2-04b: the browser-only previews never left the browser.
    expect(JSON.stringify(server.bodies)).not.toContain('data:image');
    expect(row).not.toHaveProperty('imageDataUrl');
    expect(row).not.toHaveProperty('imageDataUrls');
  });

  it('a stable syncKey: re-saving the same turn (fresh objects) writes nothing', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('photos');
    s.saveMessages(conv.id, [photoTurn(), answer()]);
    await s.flush();
    const after = writes(server, conv.id);

    // Every save in a session rebuilds these objects — the stream, the
    // reconcile, an intent patch on another row. None is a server change.
    for (let i = 0; i < 5; i += 1) {
      s.saveMessages(conv.id, wire([photoTurn(), answer()]));
      await s.flush();
    }
    expect(writes(server, conv.id)).toBe(after);
  });

  it('P2-04b holds with photos: a preview change alone is not a server change', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('photos');
    s.saveMessages(conv.id, [photoTurn()]);
    await s.flush();
    const after = writes(server, conv.id);
    s.saveMessages(conv.id, [{ ...photoTurn(), imageDataUrl: 'data:image/png;base64,BBBB' }]);
    await s.flush();
    expect(writes(server, conv.id)).toBe(after);
  });

  it('a hydrate reproduces the same key, so opening a chat pushes nothing', async () => {
    const server = makeServer();
    const writer = store(server);
    const conv = writer.create('photos');
    writer.saveMessages(conv.id, [photoTurn(), answer()]);
    await writer.flush();
    const after = writes(server, conv.id);

    const reader = store(server);
    const loaded = await reader.load(conv.id, { force: true });
    reader.saveMessages(conv.id, loaded!.messages);
    await reader.flush();
    expect(writes(server, conv.id)).toBe(after);
  });
});

describe('no older copy of the thread erases the reference', () => {
  it('a save from a copy taken before the reference keeps it', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('photos');
    s.saveMessages(conv.id, [photoTurn()]);
    await s.flush();

    // The live stream captured the turn without the field, and saves the
    // finished answer with that copy.
    const stale = { ...photoTurn(), meta: undefined };
    s.saveMessages(conv.id, [stale, answer()]);
    await s.flush();

    expect(s.get(conv.id)!.messages[0].meta?.images).toEqual(IMAGES);
    expect(server.convs.get(conv.id)!.messages[0].meta?.images).toEqual(IMAGES);
  });

  it('a refused push (409, the server wrote meanwhile) re-applies it', async () => {
    const server = makeServer();
    const s = store(server);
    const conv = s.create('photos');
    const plain: ChatMessage = { id: 'u-1', role: 'user', content: 'old photo turn', createdAt: 1 };
    s.saveMessages(conv.id, [plain, answer()]);
    await s.flush();

    // The backfill amends the turn; the server refuses the PUT once.
    server.refuseNextPut(1);
    await s.amendMessages!(conv.id, [
      { ...plain, meta: { images: [IMAGES[0]] } },
      answer(),
    ]);
    await s.flush();

    expect(server.convs.get(conv.id)!.messages[0].meta?.images).toEqual([IMAGES[0]]);
    expect(s.get(conv.id)!.messages[0].meta?.images).toEqual([IMAGES[0]]);
  });

  it('amending a thread does not move it in Recents', async () => {
    const server = makeServer();
    const s = store(server);
    const older = s.create('older');
    s.saveMessages(older.id, [{ id: 'u-o', role: 'user', content: 'old', createdAt: 1 }]);
    await new Promise((r) => setTimeout(r, 5));
    const newer = s.create('newer');
    s.saveMessages(newer.id, [{ id: 'u-n', role: 'user', content: 'new', createdAt: 2 }]);
    await s.flush();
    const before = s.get(older.id)!.updatedAt;

    await s.amendMessages!(older.id, [
      { id: 'u-o', role: 'user', content: 'old', createdAt: 1, meta: { images: [IMAGES[0]] } },
    ]);
    expect(s.get(older.id)!.updatedAt).toBe(before);
    expect(s.list().map((c) => c.id)).toEqual([newer.id, older.id]);
    expect(server.convs.get(older.id)!.messages[0].meta?.images).toEqual([IMAGES[0]]);
  });
});

describe('the second device', () => {
  it('a fresh store hydrates the turn and the row renders the server thumbnails', async () => {
    const server = makeServer();
    const phone = store(server);
    const conv = phone.create('photos');
    phone.saveMessages(conv.id, [photoTurn(), answer()]);
    await phone.flush();

    const desktop = store(server); // nothing cached, no IndexedDB, no bytes
    const loaded = await desktop.load(conv.id);
    const turn = loaded!.messages[0];
    expect(turn.imageDataUrl).toBeUndefined();
    expect(turn.meta?.images).toEqual(IMAGES);

    const { container } = render(
      <MessageRow
        message={turn}
        isLast={false}
        onRegenerate={() => undefined}
        onRetry={() => undefined}
        conversationId={conv.id}
      />,
    );
    const imgs = [...container.querySelectorAll('img')];
    expect(imgs).toHaveLength(2);
    expect(imgs[0].getAttribute('src')).toBe(
      `/api/chat-media/${conv.id}/img-aaaa-0001?size=thumb`,
    );
    expect(imgs[1].getAttribute('src')).toBe(
      `/api/chat-media/${conv.id}/img-aaaa-0002?size=thumb`,
    );
    for (const img of imgs) {
      expect(img.getAttribute('loading')).toBe('lazy');
      expect(img.getAttribute('decoding')).toBe('async');
    }
    // The box is reserved before any byte: no layout shift when it lands.
    expect(imgs[0].getAttribute('width')).toBe('1600');
    expect(imgs[0].getAttribute('height')).toBe('1200');
    expect(imgs[0].style.width).toBe('213px');
    expect(imgs[0].style.aspectRatio).toBe('1600 / 1200');
    expect(imgs[1].style.width).toBe('160px');
    // Nothing anywhere points at a data URL — there is none on this device.
    expect(container.innerHTML).not.toContain('data:image');
  });
});
