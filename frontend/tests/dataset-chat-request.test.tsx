// @vitest-environment jsdom
/**
 * NEW-14 — a dataset sent WITHOUT a typed prompt must still produce a valid
 * chat request.
 *
 * The bug, in one line: images get IMAGE_ONLY_PROMPT, documents get
 * PDF_ONLY_PROMPT, and datasets got nothing — so a .csv/.xlsx dropped in with
 * no question uploaded and profiled perfectly (POST /api/upload → 200) and
 * then died at the proxy, which returned 400 "no user message or image in
 * request" without ever calling the orchestrator. The user saw
 * "Something went wrong" over a file the server had already read.
 *
 * The second bug is in the same three lines and is worse, because it does not
 * fail loudly. `startStream` drops empty-content messages from the transcript
 * it posts, so an attachment-only turn LEAVES NO TRACE in `messages`. Asking
 * "what was the last thing a user said?" then walks backwards past the turn
 * being sent and finds the PREVIOUS question — so attaching a spreadsheet to a
 * chat that once asked "What is Python?" re-answered "What is Python?", with
 * the spreadsheet quietly in scope. A wrong answer beats a 400 for damage.
 *
 * These tests therefore assert two separate things, and both must hold:
 *
 *   1. a dataset-only turn maps to DATASET_ONLY_PROMPT, and
 *   2. the CURRENT turn decides the message — never an older one.
 *
 * The last block is the one that would actually have caught this in CI. Every
 * existing dataset test mocks `startStream`, which is exactly why thirteen of
 * them passed while the real browser failed: the body that gets posted was
 * never built, so the translation that rejected it was never run. The
 * end-to-end block below drives the REAL ChatApp through the REAL startStream,
 * captures the REAL /api/chat body off the wire, and feeds it to the REAL
 * translation the proxy uses. Delete DATASET_ONLY_PROMPT support and it fails.
 */

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DATASET_ONLY_PROMPT,
  IMAGE_ONLY_PROMPT,
  PDF_ONLY_PROMPT,
  currentUserContent,
  toOrchestratorChatRequest,
  type ChatRequestBody,
} from '@/lib/orchestrator';
import type { ChatMessage } from '@/lib/types';

/* ======================================================================
   PART 1 — the translation contract (frontend body → orchestrator body).

   Pure functions, no DOM. This is the exact code path app/api/chat/route.ts
   runs before it decides whether to forward or to answer 400.
   ====================================================================== */

describe('NEW-14 · the current turn decides the message', () => {
  it('reads the turn being sent, not the newest one with text in it', () => {
    // The transcript a normal text send posts: it ENDS with the turn being
    // asked, which is what makes "the last message" a sound definition.
    expect(
      currentUserContent({
        messages: [
          { role: 'user', content: 'old question' },
          { role: 'assistant', content: 'old answer' },
          { role: 'user', content: 'summarise sheet' },
        ],
      }),
    ).toBe('summarise sheet');
  });

  it('is EMPTY when the current turn carried no text, even mid-conversation', () => {
    // An attachment-only turn is filtered out of `messages` by startStream, so
    // the transcript ends on the assistant. That trailing assistant IS the
    // evidence that the current turn said nothing — walking further back to
    // "old question" is the bug this helper exists to prevent.
    expect(
      currentUserContent({
        messages: [
          { role: 'user', content: 'What is Python?' },
          { role: 'assistant', content: 'A language.' },
        ],
      }),
    ).toBe('');
  });

  it('is empty for an empty or absent transcript', () => {
    expect(currentUserContent({ messages: [] })).toBe('');
    expect(currentUserContent({})).toBe('');
  });

  it('believes an explicit empty current turn over the transcript tail', () => {
    // The hole that positional reading alone cannot close: an ASSISTANT turn
    // can be empty too — a generation stopped before its first token, or the
    // failed turn NEW-14 itself produces — so it is filtered out as well, and
    // the transcript ends on the previous QUESTION. Inference inverts here;
    // the stated value must win, and `''` is a value.
    expect(
      currentUserContent({
        messages: [{ role: 'user', content: 'What is Python?' }],
        current_text: '',
      }),
    ).toBe('');
  });

  it('prefers the stated current turn over a trailing user message', () => {
    expect(
      currentUserContent({
        messages: [{ role: 'user', content: 'stale' }],
        current_text: 'the real question',
      }),
    ).toBe('the real question');
  });

  it('falls back to position when the field is absent (older bodies)', () => {
    expect(
      currentUserContent({
        messages: [{ role: 'user', content: 'only what is here' }],
      }),
    ).toBe('only what is here');
  });

  it('trims, so whitespace is not mistaken for a question', () => {
    expect(
      currentUserContent({ messages: [{ role: 'user', content: '  \n ' }] }),
    ).toBe('');
  });
});

describe('NEW-14 · dataset-only sends', () => {
  it('CASE 1 — XLSX with no typed text becomes DATASET_ONLY_PROMPT', () => {
    const out = toOrchestratorChatRequest({
      messages: [],
      session_id: 'conv-xlsx',
      conversation_id: 'conv-xlsx',
      dataset: true,
    });
    expect(out).not.toBeNull();
    expect(out?.message).toBe(DATASET_ONLY_PROMPT);
    expect(out?.message.trim().length).toBeGreaterThan(0);
  });

  it('CASE 2 — CSV with no typed text behaves identically', () => {
    const out = toOrchestratorChatRequest({
      messages: [],
      session_id: 'conv-csv',
      conversation_id: 'conv-csv',
      dataset: true,
    });
    expect(out?.message).toBe(DATASET_ONLY_PROMPT);
  });

  it('CASE 3 — typed text wins over the fallback', () => {
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'summarise this spreadsheet' }],
      session_id: 'conv-1',
      dataset: true,
    });
    expect(out?.message).toBe('summarise this spreadsheet');
    expect(out?.message).not.toBe(DATASET_ONLY_PROMPT);
  });

  it('CASE 4 — an older question is NEVER reused for an attachment-only turn', () => {
    const out = toOrchestratorChatRequest({
      messages: [
        { role: 'user', content: 'What is Python?' },
        { role: 'assistant', content: 'A programming language.' },
      ],
      current_text: '',
      session_id: 'conv-2',
      dataset: true,
    });
    // The regression, stated as plainly as it can be: this must not be the
    // previous question, and it must not be a 400 either.
    expect(out).not.toBeNull();
    expect(out?.message).not.toBe('What is Python?');
    expect(out?.message).toBe(DATASET_ONLY_PROMPT);
    // The transcript itself is untouched — the model still gets the history.
    expect(out?.messages).toEqual([
      { role: 'user', content: 'What is Python?' },
      { role: 'assistant', content: 'A programming language.' },
    ]);
  });

  it('CASE 4 — survives the recovery path this bug itself creates', () => {
    // Retrying after NEW-14 fired: the failed turn left an EMPTY assistant
    // message behind (observed in production as `assistant | length 0`), so
    // the filtered transcript ends on the old QUESTION rather than on an
    // assistant turn. This is the shape that defeats positional inference.
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'What is Python?' }],
      current_text: '',
      session_id: 'conv-3',
      dataset: true,
    });
    expect(out?.message).toBe(DATASET_ONLY_PROMPT);
    expect(out?.message).not.toBe('What is Python?');
  });

  it('forwards neither `dataset` nor `current_text` — both are proxy-only', () => {
    // The signals exist so this function can pick a prompt. The orchestrator
    // finds the dataset through conversation_id → uploads, exactly as before,
    // and its ChatRequest gains no field — so a body carrying both must
    // produce the SAME key set a v1 body would.
    const out = toOrchestratorChatRequest({
      messages: [{ role: 'user', content: 'hello' }],
      current_text: 'hello',
      session_id: 's',
      dataset: true,
    });
    expect(Object.keys(out!).sort()).toEqual([
      'image_base64',
      'message',
      'messages',
      'session_id',
    ]);
  });

  it('leaves image_base64 null and sends no pdf for a dataset turn', () => {
    const out = toOrchestratorChatRequest({
      messages: [],
      session_id: 's',
      dataset: true,
    });
    expect(out?.image_base64).toBeNull();
    expect(out && 'pdf' in out).toBe(false);
  });
});

describe('NEW-14 · everything that already worked still works', () => {
  it('CASE 5a — image-only still uses IMAGE_ONLY_PROMPT', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [],
        session_id: 's',
        image: 'aW1n',
      })?.message,
    ).toBe(IMAGE_ONLY_PROMPT);
  });

  it('CASE 5a — image-only mid-conversation does not reuse the old question', () => {
    // The same second-order bug lived here; it was simply never reported,
    // because an image attached to "What is Python?" still looks like an
    // answer to something.
    expect(
      toOrchestratorChatRequest({
        messages: [
          { role: 'user', content: 'What is Python?' },
          { role: 'assistant', content: 'A language.' },
        ],
        session_id: 's',
        image: 'aW1n',
      })?.message,
    ).toBe(IMAGE_ONLY_PROMPT);
  });

  it('CASE 5b — PDF-only still uses PDF_ONLY_PROMPT', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [],
        session_id: 's',
        pdf: 'JVBER',
        pdf_filename: 'spec.pdf',
      })?.message,
    ).toBe(PDF_ONLY_PROMPT);
  });

  it('an image outranks a dataset flag, so the ordering is unchanged', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [],
        session_id: 's',
        image: 'aW1n',
        dataset: true,
      })?.message,
    ).toBe(IMAGE_ONLY_PROMPT);
  });

  it('CASE 7 — an ordinary text-only request is untouched', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [
          { role: 'user', content: 'first' },
          { role: 'assistant', content: 'answer' },
          { role: 'user', content: 'how many accounts?' },
        ],
        session_id: 's',
      }),
    ).toEqual({
      message: 'how many accounts?',
      messages: [
        { role: 'user', content: 'first' },
        { role: 'assistant', content: 'answer' },
        { role: 'user', content: 'how many accounts?' },
      ],
      session_id: 's',
      image_base64: null,
    });
  });

  it('CASE 8 — nothing at all is STILL rejected', () => {
    // The fallback must be earned. A request with no text and no attachment
    // has to keep failing, or the 400 that protects the orchestrator from
    // meaningless generations is gone.
    expect(toOrchestratorChatRequest({})).toBeNull();
    expect(
      toOrchestratorChatRequest({ messages: [], session_id: 's' }),
    ).toBeNull();
    expect(
      toOrchestratorChatRequest({
        messages: [
          { role: 'user', content: 'What is Python?' },
          { role: 'assistant', content: 'A language.' },
        ],
        session_id: 's',
      }),
    ).toBeNull();
  });

  it('CASE 8 — `dataset: false` earns nothing', () => {
    expect(
      toOrchestratorChatRequest({
        messages: [],
        session_id: 's',
        dataset: false,
      }),
    ).toBeNull();
  });

  it('CASE 9 — a clarification answer with no text of its own is unchanged', () => {
    const out = toOrchestratorChatRequest({
      messages: [],
      session_id: 's',
      clarification: { clarification_id: 'c1', skipped: true },
    });
    expect(out).not.toBeNull();
    expect(out?.message).toBe('');
    expect(out?.clarification).toEqual({
      clarification_id: 'c1',
      skipped: true,
    });
  });
});

/* ======================================================================
   PART 2 — the boundary the mocked tests never crossed.

   ChatApp → startStream (REAL) → the JSON actually posted to /api/chat →
   toOrchestratorChatRequest (REAL). Nothing between the send button and the
   translation is stubbed except the network itself.
   ====================================================================== */

let stored: ChatMessage[] = [];

vi.mock('@/lib/history', () => ({
  newId: () => `m${Math.random().toString(36).slice(2, 10)}`,
  setEvictListener: () => undefined,
  rebuildHistoryStore: async () => {
    throw new Error('unexpected account switch in test');
  },
  getHistoryStore: () => ({
    ready: async () => undefined,
    list: () => [],
    listArchived: () => [],
    get: () => null,
    create: (title: string) => ({
      id: 'conv-1',
      title,
      messages: [],
      createdAt: 0,
      updatedAt: 0,
    }),
    saveMessages: (_id: string, msgs: ChatMessage[]) => {
      stored = msgs;
    },
    load: async () => null,
    setActiveUser: () => false,
    wipeLocal: async () => undefined,
    migrateLocalConversations: async () => 0,
    refresh: async () => true,
    refreshArchived: async () => true,
    generateTitle: async () => undefined,
    truncateMessages: async () => undefined,
    setMessageFeedback: async () => undefined,
    exportMarkdown: async () => null,
    remove: () => undefined,
    rename: () => undefined,
    setPinned: () => undefined,
    setArchived: () => undefined,
  }),
}));

vi.mock('@/lib/auth', () => ({
  fetchMe: async () => ({ ok: true, username: 'tester', user: null }),
  userScopeKey: (me: { user: { id: number } | null; username: string }) =>
    me.user ? `u${me.user.id}` : me.username,
  redirectToLogin: () => undefined,
}));
vi.mock('@/lib/salesforceApi', () => ({
  fetchSalesforceContext: async () => ({ options: [], pending: null }),
  cancelClarification: async () => undefined,
  shouldShowStarter: () => false,
}));
vi.mock('@/lib/compact', () => ({
  isCompacting: () => false,
  requestCompact: async () => null,
}));

const { ChatApp } = await import('@/components/ChatApp');

/** A one-event SSE stream, so the real `consume()` finishes cleanly. */
function sseBody(): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode('event: done\ndata: {}\n\n'),
      );
      controller.close();
    },
  });
}

/** Every /api/chat body posted during a test, in order. */
let chatBodies: ChatRequestBody[] = [];
let uploadCalls: number;

function stubNetwork() {
  chatBodies = [];
  uploadCalls = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/upload')) {
        uploadCalls += 1;
        return {
          ok: true,
          status: 200,
          json: async () => ({ upload_id: 'up-1', files: 1 }),
        };
      }
      if (String(url).startsWith('/api/chat')) {
        chatBodies.push(JSON.parse(String(init?.body)) as ChatRequestBody);
        return { ok: true, status: 200, body: sseBody() };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    }),
  );
}

function stubBrowserApis() {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => undefined);
  HTMLMediaElement.prototype.play =
    HTMLMediaElement.prototype.play ?? (async () => undefined);
}

/** Attach `name` and press Send, optionally typing `text` first. */
function sendWith(name: string, text: string, type: string) {
  const input = document.querySelector(
    'input[type="file"]',
  ) as HTMLInputElement;
  const file = new File(['col_a,col_b\n1,2\n'], name, { type });
  fireEvent.change(input, { target: { files: [file] } });
  if (text) {
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: text },
    });
  }
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
}

beforeEach(() => {
  stubBrowserApis();
  stubNetwork();
  stored = [];
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('NEW-14 · end to end, through the real request builder', () => {
  it('CASE 10 — a dataset-only send posts a body the proxy accepts', async () => {
    render(<ChatApp />);
    sendWith('Bug Fixing Status (1).xlsx', '', 'application/vnd.ms-excel');

    // The upload must still happen first, and exactly once (H-01/H-02).
    await waitFor(() => expect(uploadCalls).toBe(1));
    await waitFor(() => expect(chatBodies.length).toBe(1));

    const body = chatBodies[0];
    // The two signals the proxy needs, and the reason it needs them: the
    // attachment-only turn is genuinely absent from the transcript.
    expect(body.dataset).toBe(true);
    expect(body.current_text).toBe('');
    expect(body.messages).toEqual([]);

    // …and the translation the proxy actually runs now succeeds.
    const req = toOrchestratorChatRequest(body);
    expect(req).not.toBeNull();
    expect(req?.message).toBe(DATASET_ONLY_PROMPT);
    expect(req?.conversation_id).toBe('conv-1');
  });

  it('a dataset-only send in an EXISTING chat does not re-ask the old question', async () => {
    render(<ChatApp />);

    // Turn one: an ordinary question, answered.
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'What is Python?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await waitFor(() => expect(chatBodies.length).toBe(1));
    await waitFor(() => expect(stored.length).toBeGreaterThan(1));

    // Turn two: a spreadsheet, no words.
    sendWith('report.xlsx', '', 'application/vnd.ms-excel');
    await waitFor(() => expect(chatBodies.length).toBe(2));

    const req = toOrchestratorChatRequest(chatBodies[1]);
    expect(req).not.toBeNull();
    expect(req?.message).not.toBe('What is Python?');
    expect(req?.message).toBe(DATASET_ONLY_PROMPT);
  });

  it('a dataset send WITH text posts that text, not the fallback', async () => {
    render(<ChatApp />);
    sendWith('report.csv', 'summarise this spreadsheet', 'text/csv');

    await waitFor(() => expect(chatBodies.length).toBe(1));
    const req = toOrchestratorChatRequest(chatBodies[0]);
    expect(req?.message).toBe('summarise this spreadsheet');
  });

  it('a text-only send carries no dataset signal at all', async () => {
    render(<ChatApp />);
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'hello' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => expect(chatBodies.length).toBe(1));
    expect(chatBodies[0].dataset).toBeUndefined();
    expect(uploadCalls).toBe(0);
    expect(toOrchestratorChatRequest(chatBodies[0])?.message).toBe('hello');
  });

  it('a FAILED upload still starts no generation (H-02 unchanged)', async () => {
    // The fallback must not become a way to generate from a dataset the
    // server rejected.
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url).startsWith('/api/upload')) {
          uploadCalls += 1;
          return {
            ok: false,
            status: 400,
            json: async () => ({ detail: 'That file could not be read.' }),
          };
        }
        if (String(url).startsWith('/api/chat')) {
          chatBodies.push(JSON.parse(String(init?.body)) as ChatRequestBody);
          return { ok: true, status: 200, body: sseBody() };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      }),
    );

    render(<ChatApp />);
    sendWith('broken.csv', '', 'text/csv');

    await waitFor(() => expect(uploadCalls).toBe(1));
    // Give any stray generation a chance to appear before declaring it absent.
    await new Promise((r) => setTimeout(r, 50));
    expect(chatBodies).toEqual([]);
  });
});
