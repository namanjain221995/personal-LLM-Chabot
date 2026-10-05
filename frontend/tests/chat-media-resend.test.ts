/**
 * Chat media on the wire (2026-10-02, docs/chat-media/CONTRACT.md §5 and §10).
 *
 *   · a send carries `image_ids` beside its inline photos, so the server
 *     stores each under the id the turn's `meta.images` names;
 *   · a regenerate, edit or retry on a device that never held the photo
 *     sends `image_refs` instead of bytes (RC-3b: it used to send nothing at
 *     all, silently, and the model was re-asked without the picture);
 *   · a 422 `image_ref_missing` — the server no longer has that photo — is
 *     relayed by the chat proxy as `{code, missing}` and withdraws the send,
 *     so the person is asked to attach it again rather than shown an error
 *     whose Retry can only fail the same way.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '@/lib/types';

const saved: Array<{ id: string; messages: ChatMessage[] }> = [];
let storedThread: ChatMessage[] | null = null;

vi.mock('@/lib/history', () => ({
  newId: () => `m${Math.random().toString(36).slice(2, 10)}`,
  getHistoryStore: () => ({
    get: () => (storedThread ? { messages: storedThread } : null),
    saveMessages: (id: string, messages: ChatMessage[]) => {
      saved.push({ id, messages });
    },
  }),
}));

const { getLiveStream, startStream } = await import('@/lib/streams');
const {
  attachmentsForResend,
  clearAttachments,
  rememberAttachments,
  resendOptionsFor,
} = await import('@/lib/attachments');
const { toOrchestratorChatRequest, IMAGE_ONLY_PROMPT } = await import('@/lib/orchestrator');
const { POST: chatRoute } = await import('@/app/api/chat/route');

const PNG = 'data:image/png;base64,iVBORw0KGgo=';
const PREFS = { model: 'smart', effort: 'medium', mode: 'assistant' } as never;

beforeEach(() => {
  saved.length = 0;
  storedThread = null;
  clearAttachments();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

function user(extra: Partial<ChatMessage> = {}): ChatMessage {
  return { id: 'u1', role: 'user', content: 'is this healthy?', createdAt: 1, ...extra };
}

/* ======================================================= what a resend sends */

describe('resendOptionsFor — photos', () => {
  it('sends this tab’s own bytes with their ids when it has them', () => {
    rememberAttachments('u1', [
      { kind: 'image', name: 'a.png', base64: 'AAAA', attachment_id: 'img-aaaa-0001' },
    ]);
    const out = resendOptionsFor(
      user({ meta: { images: [{ attachment_id: 'img-aaaa-0001' }] } }),
    );
    expect(out.images).toEqual(['AAAA']);
    expect(out.imageIds).toEqual(['img-aaaa-0001']);
    expect(out.imageRefs).toBeNull();
    expect(out.missing).toBe(false);
  });

  it('pairs persisted previews with the stored ids when the counts agree', () => {
    const out = resendOptionsFor(
      user({
        imageDataUrl: PNG,
        meta: { images: [{ attachment_id: 'img-aaaa-0001' }] },
      }),
    );
    expect(out.images).toEqual(['iVBORw0KGgo=']);
    expect(out.imageIds).toEqual(['img-aaaa-0001']);
    expect(out.imageRefs).toBeNull();
  });

  it('sends no ids at all rather than a partial or mismatched list', () => {
    const out = resendOptionsFor(
      user({
        imageDataUrls: [PNG, PNG],
        meta: { images: [{ attachment_id: 'img-aaaa-0001' }] },
      }),
    );
    expect(out.images).toHaveLength(2);
    expect(out.imageIds).toBeNull();
  });

  it('RC-3b: with no bytes here, names the stored photos instead — never nothing', () => {
    const out = resendOptionsFor(
      user({
        meta: {
          images: [{ attachment_id: 'img-aaaa-0001' }, { attachment_id: 'img-aaaa-0002' }],
        },
      }),
    );
    expect(out.images).toEqual([]);
    expect(out.imageRefs).toEqual(['img-aaaa-0001', 'img-aaaa-0002']);
    expect(out.missing).toBe(false);
    expect(attachmentsForResend(user({ meta: { images: [{ attachment_id: 'img-aaaa-0001' }] } })).imageRefs)
      .toEqual(['img-aaaa-0001']);
  });

  it('keeps today’s behaviour for a turn with neither', () => {
    const out = resendOptionsFor(user());
    expect(out.images).toEqual([]);
    expect(out.imageRefs).toBeNull();
    expect(out.missing).toBe(false);
    // An unreadable preview with nothing stored is still `missing`.
    const broken = resendOptionsFor(user({ imageDataUrl: 'blob:nope' }));
    expect(broken.missing).toBe(true);
  });
});

/* ===================================================== the request body */

async function bodyOf(opts: Partial<Parameters<typeof startStream>[0]>): Promise<Record<string, unknown>> {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
    async () => new Response(null, { status: 503 }),
  );
  vi.stubGlobal('fetch', fetchMock);
  await startStream({ conversationId: 'conv-wire-1', turns: [user()], prefs: PREFS, ...opts });
  return JSON.parse(fetchMock.mock.calls[0][1]?.body as string);
}

describe('startStream — the /api/chat body', () => {
  it('carries image_ids index for index with the photos', async () => {
    const body = await bodyOf({ images: ['AAAA', 'BBBB'], imageIds: ['img-aaaa-0001', 'img-aaaa-0002'] });
    expect(body.images).toEqual(['AAAA', 'BBBB']);
    expect(body.image_ids).toEqual(['img-aaaa-0001', 'img-aaaa-0002']);
  });

  it('drops ids that do not pair with the photos, and keeps every other body byte-identical', async () => {
    const mismatched = await bodyOf({ images: ['AAAA'], imageIds: ['img-aaaa-0001', 'img-aaaa-0002'] });
    expect(mismatched).not.toHaveProperty('image_ids');
    const plain = await bodyOf({});
    expect(plain).not.toHaveProperty('image_ids');
    expect(plain).not.toHaveProperty('image_refs');
  });

  it('carries image_refs for photos sent by reference', async () => {
    const body = await bodyOf({ imageRefs: ['img-aaaa-0001'] });
    expect(body.image_refs).toEqual(['img-aaaa-0001']);
    expect(body).not.toHaveProperty('image');
  });
});

describe('the proxy translation', () => {
  it('forwards well-formed ids only, all or nothing', () => {
    const base = { messages: [{ role: 'user', content: 'q' }], session_id: 's' };
    expect(
      toOrchestratorChatRequest({ ...base, image: 'AAAA', image_ids: ['img-aaaa-0001'] })?.image_ids,
    ).toEqual(['img-aaaa-0001']);
    expect(
      toOrchestratorChatRequest({ ...base, image: 'AAAA', image_ids: ['img-aaaa-0001', '../x'] }),
    ).not.toHaveProperty('image_ids');
    expect(
      toOrchestratorChatRequest({ ...base, image_refs: ['img-aaaa-0001', 'img-aaaa-0002'] })?.image_refs,
    ).toEqual(['img-aaaa-0001', 'img-aaaa-0002']);
    expect(toOrchestratorChatRequest({ ...base, image_refs: ['short'] })).not.toHaveProperty('image_refs');
    // No limit since 2026-10-03 (LIMITS.md), only the 999 technical ceiling:
    // 100 and 999 pass, 1000 does not.
    for (const n of [100, 999]) {
      expect(
        toOrchestratorChatRequest({ ...base, image_refs: Array(n).fill('img-aaaa-0001') })
          ?.image_refs,
      ).toHaveLength(n);
    }
    expect(
      toOrchestratorChatRequest({ ...base, image_refs: Array(1000).fill('img-aaaa-0001') }),
    ).not.toHaveProperty('image_refs');
    // No photo fields in, no photo fields out: the v1 key set is untouched.
    const plain = toOrchestratorChatRequest(base)!;
    expect(Object.keys(plain).sort()).toEqual(['image_base64', 'message', 'messages', 'session_id']);
  });

  it('a wordless resend by reference still asks about the picture', () => {
    const req = toOrchestratorChatRequest({
      messages: [],
      current_text: '',
      session_id: 's',
      image_refs: ['img-aaaa-0001'],
    });
    expect(req?.message).toBe(IMAGE_ONLY_PROMPT);
    expect(req?.image_base64).toBeNull();
  });
});

/* =================================================== 422 image_ref_missing */

describe('a stored photo the server can no longer load', () => {
  it('the chat proxy relays the code and the ids — and nothing else upstream said', async () => {
    vi.stubEnv('ORCHESTRATOR_URL', 'http://orch.test');
    vi.stubEnv('MOCK_MODE', 'false');
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json(
          {
            detail: {
              code: 'image_ref_missing',
              missing: ['img-aaaa-0001', '<script>'],
              note: 'upstream prose',
            },
          },
          { status: 422 },
        ),
      ),
    );
    const res = await chatRoute(
      new Request('http://x/api/chat', {
        method: 'POST',
        body: JSON.stringify({
          messages: [{ role: 'user', content: 'q' }],
          session_id: 's',
          image_refs: ['img-aaaa-0001'],
        }),
      }),
    );
    expect(res.status).toBe(422);
    expect(await res.json()).toEqual({ code: 'image_ref_missing', missing: ['img-aaaa-0001'] });
  });

  it('any other 422 is still only a category', async () => {
    vi.stubEnv('ORCHESTRATOR_URL', 'http://orch.test');
    vi.stubEnv('MOCK_MODE', 'false');
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ detail: 'bad shape' }, { status: 422 })),
    );
    const res = await chatRoute(
      new Request('http://x/api/chat', {
        method: 'POST',
        body: JSON.stringify({ messages: [{ role: 'user', content: 'q' }], session_id: 's' }),
      }),
    );
    expect(res.status).toBe(422);
    expect(await res.json()).toEqual({ code: 'APPLICATION_ERROR' });
  });

  it('withdraws the send and asks for the photo, instead of an error row', async () => {
    const thread: ChatMessage[] = [
      user({ meta: { images: [{ attachment_id: 'img-aaaa-0001' }] } }),
      { id: 'a1', role: 'assistant', content: 'It looks healthy.', createdAt: 2 },
    ];
    storedThread = thread;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({ code: 'image_ref_missing', missing: ['img-aaaa-0001'] }, { status: 422 }),
      ),
    );
    const onImagesMissing = vi.fn();
    await startStream({
      conversationId: 'conv-wire-422',
      turns: thread,
      context: [thread[0]],
      prefs: PREFS,
      intentId: 'intent-aaaa-0001',
      imageRefs: ['img-aaaa-0001'],
      onImagesMissing,
    });
    expect(onImagesMissing).toHaveBeenCalledWith(['img-aaaa-0001']);
    const view = getLiveStream('conv-wire-422');
    expect(view?.status).toBe('stopped');
    // The thread is what it was before the click: no placeholder, no error.
    expect(view?.messages).toBe(thread);
    expect(view?.messages.some((m) => m.status === 'error')).toBe(false);
    // Nothing of the withdrawn attempt was written to history.
    expect(saved).toHaveLength(0);
  });

  it('a 422 for a send WITHOUT refs is still an ordinary failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ code: 'APPLICATION_ERROR' }, { status: 422 })),
    );
    const onImagesMissing = vi.fn();
    await startStream({
      conversationId: 'conv-wire-422b',
      turns: [user()],
      prefs: PREFS,
      onImagesMissing,
    });
    expect(onImagesMissing).not.toHaveBeenCalled();
    const last = getLiveStream('conv-wire-422b')?.messages.at(-1);
    expect(last?.status).toBe('error');
    expect(last?.errorStatus).toBe(422);
  });
});
