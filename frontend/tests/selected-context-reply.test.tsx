// @vitest-environment jsdom
/**
 * "Ask TechSara AI" end to end (2026-09-03).
 *
 * REAL ChatApp → REAL startStream → the JSON actually posted to /api/chat.
 * Only the network is stubbed, for the same reason PART 2 of
 * tests/dataset-chat-request.test.tsx exists: a suite that mocks `startStream`
 * never builds the body, so it can pass while the browser sends the wrong
 * thing. The claim being made here — "the excerpt reaches the model" — is
 * exactly the claim a mocked send cannot support, so it is asserted against
 * the bytes on the wire.
 *
 * The other half is the UI contract around the reference: what keeps it, what
 * clears it, and what must never touch it.
 */

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatRequestBody } from '@/lib/orchestrator';
import type { ChatMessage } from '@/lib/types';

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
  userScopeKey: () => 'tester',
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
const { Providers } = await import('@/components/Providers');

/** One token then done, so the answer has prose worth selecting. */
function sseBody(text: string): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      const enc = new TextEncoder();
      controller.enqueue(
        enc.encode(`event: token\ndata: ${JSON.stringify({ text })}\n\n`),
      );
      controller.enqueue(enc.encode('event: done\ndata: {}\n\n'));
      controller.close();
    },
  });
}

const ANSWER = 'model drift happens when production data changes over time';

let chatBodies: ChatRequestBody[] = [];

function stubNetwork() {
  chatBodies = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      if (u.startsWith('/api/chat')) {
        chatBodies.push(JSON.parse(String(init?.body)) as ChatRequestBody);
        return { ok: true, status: 200, body: sseBody(ANSWER) };
      }
      if (u.startsWith('/api/upload')) {
        return { ok: true, status: 200, json: async () => ({ upload_id: 'u1', files: 1 }) };
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
  // jsdom does not implement Range.getBoundingClientRect at all.
  Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
    configurable: true,
    writable: true,
    value: () => ({
      top: 300, bottom: 320, left: 200, right: 400, width: 200, height: 20,
      x: 200, y: 300, toJSON: () => ({}),
    }),
  });
}

const box = () => screen.getByRole('textbox', { name: 'Message' });
const sendButton = () => screen.getByRole('button', { name: 'Send message' });

function type(text: string) {
  fireEvent.change(box(), { target: { value: text } });
}

function send(text: string) {
  type(text);
  fireEvent.click(sendButton());
}

/** Select all the text inside `el` and let SelectionAsk evaluate it. */
async function selectInside(el: Element) {
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection()!;
  sel.removeAllRanges();
  sel.addRange(range);
  await act(async () => {
    document.dispatchEvent(new Event('pointerup'));
    // SelectionAsk defers the read to a rAF so it sees the SETTLED selection
    // rather than the one that existed mid-gesture. jsdom runs rAF on a real
    // ~16ms timer, so a 0ms tick would return before the read happened.
    await new Promise((r) => setTimeout(r, 40));
  });
}

const askButton = () =>
  screen.queryByRole('button', { name: /Ask TechSara AI about the selected text/i });

/** Send one turn and wait for the streamed answer to land. */
async function seedConversation() {
  render(
    <Providers>
      <ChatApp />
    </Providers>,
  );
  send('what is drift?');
  await screen.findByText(ANSWER);
  chatBodies = [];
}

const assistantProse = () =>
  document.querySelector('[data-chat-message-role="assistant"]') as HTMLElement;
const userProse = () =>
  document.querySelector('[data-chat-message-role="user"]') as HTMLElement;

beforeEach(() => {
  stubBrowserApis();
  stubNetwork();
  stored = [];
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(() => {
  cleanup();
  window.getSelection()?.removeAllRanges();
  delete (Range.prototype as Partial<Range>).getBoundingClientRect;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/* ============================================ capturing the reference */

describe('REPLY · capturing an excerpt', () => {
  it('REPLY-01 · selecting assistant prose offers the action', async () => {
    await seedConversation();
    expect(askButton()).toBeNull();
    await selectInside(assistantProse());
    expect(askButton()).not.toBeNull();
  });

  it('REPLY-02 · selecting the user\'s own words offers it too', async () => {
    await seedConversation();
    await selectInside(userProse());
    expect(askButton()).not.toBeNull();
  });

  it('REPLY-04 · selecting chrome (the composer\'s own label) offers nothing', async () => {
    await seedConversation();
    const label = screen.getByRole('button', { name: 'Send message' });
    await selectInside(label);
    expect(askButton()).toBeNull();
  });

  it('REPLY-07/08/10 · clicking it makes a reference card and focuses the box', async () => {
    await seedConversation();
    await selectInside(assistantProse());
    await act(async () => {
      fireEvent.click(askButton()!);
    });

    // The excerpt is now on screen TWICE — in the answer it came from and in
    // the card — so the assertion is scoped to the card rather than to the
    // document, which would match the source paragraph just as happily.
    const card = screen.getByText('Replying to').closest('div')!;
    expect(card.textContent).toContain(ANSWER);
    expect(
      screen.getByRole('button', { name: 'Remove selected context' }),
    ).toBeTruthy();
    // The action itself is gone; the reference has taken its place.
    expect(askButton()).toBeNull();
    expect(document.activeElement).toBe(box());
  });

  it('REPLY-09 · × removes the reference and leaves the typed text alone', async () => {
    await seedConversation();
    await selectInside(assistantProse());
    await act(async () => {
      fireEvent.click(askButton()!);
    });
    type('why does this happen?');

    fireEvent.click(screen.getByRole('button', { name: 'Remove selected context' }));

    expect(screen.queryByText('Replying to')).toBeNull();
    expect((box() as HTMLTextAreaElement).value).toBe('why does this happen?');
  });

  it('REPLY-20 · attaching a file does not disturb the reference', async () => {
    await seedConversation();
    await selectInside(assistantProse());
    await act(async () => {
      fireEvent.click(askButton()!);
    });
    type('why?');

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, {
        target: { files: [new File(['a,b\n1,2\n'], 'data.csv', { type: 'text/csv' })] },
      });
    });

    expect(await screen.findByLabelText('Remove attachment data.csv')).toBeTruthy();
    expect(screen.getByText('Replying to')).toBeTruthy();
    expect((box() as HTMLTextAreaElement).value).toBe('why?');
  });

  it('REPLY-18 · Escape dismisses the action and does NOT stop the stream', async () => {
    await seedConversation();
    await selectInside(assistantProse());
    expect(askButton()).not.toBeNull();

    await act(async () => {
      fireEvent.keyDown(window, { key: 'Escape' });
    });
    expect(askButton()).toBeNull();
    // Nothing was sent, aborted or otherwise acted on — the answer is intact.
    expect(screen.getByText(ANSWER)).toBeTruthy();
  });

  it('Escape leaves a COMMITTED reference alone — it took a click to make', async () => {
    await seedConversation();
    await selectInside(assistantProse());
    await act(async () => {
      fireEvent.click(askButton()!);
    });
    await act(async () => {
      fireEvent.keyDown(window, { key: 'Escape' });
    });
    expect(screen.getByText('Replying to')).toBeTruthy();
  });
});

/* ================================================== sending with a quote */

describe('REPLY · what reaches the model', () => {
  async function askAbout(excerptHost: Element, question: string) {
    await selectInside(excerptHost);
    await act(async () => {
      fireEvent.click(askButton()!);
    });
    await act(async () => {
      send(question);
      await new Promise((r) => setTimeout(r, 0));
    });
  }

  it('REPLY-12 · the excerpt AND the follow-up are both on the wire', async () => {
    await seedConversation();
    await askAbout(assistantProse(), 'why does this happen?');

    await waitFor(() => expect(chatBodies.length).toBe(1));
    const body = chatBodies[0];
    const turn = body.messages!.filter((m) => m.role === 'user').pop()!;

    expect(turn.content).toContain('Selected context from a previous assistant message:');
    expect(turn.content).toContain(`> ${ANSWER}`);
    expect(turn.content).toContain('User follow-up:');
    expect(turn.content).toContain('why does this happen?');
    // `current_text` is what the proxy reads for a wordless turn; it must
    // carry the same folded string, not the bare question.
    expect(body.current_text).toBe(turn.content);
  });

  it('quotes the user role correctly when the excerpt was their own message', async () => {
    await seedConversation();
    await askAbout(userProse(), 'expand on that');

    await waitFor(() => expect(chatBodies.length).toBe(1));
    const turn = chatBodies[0].messages!.filter((m) => m.role === 'user').pop()!;
    expect(turn.content).toContain('Selected context from a previous user message:');
    expect(turn.content).toContain('> what is drift?');
  });

  it('REPLY-11 · a send with NO quote is byte-identical to the old flow', async () => {
    await seedConversation();
    await act(async () => {
      send('plain follow-up');
      await new Promise((r) => setTimeout(r, 0));
    });

    await waitFor(() => expect(chatBodies.length).toBe(1));
    const turn = chatBodies[0].messages!.filter((m) => m.role === 'user').pop()!;
    expect(turn.content).toBe('plain follow-up');
    expect(chatBodies[0].current_text).toBe('plain follow-up');
  });

  it('REPLY-13/23 · the sent turn stores the reference and renders it', async () => {
    await seedConversation();
    await askAbout(assistantProse(), 'why does this happen?');

    // Stored on meta, which the server round-trips verbatim — this is what a
    // reload reads back.
    await waitFor(() => {
      const asked = stored.filter((m) => m.role === 'user').pop()!;
      expect(asked.meta?.selected_context).toEqual({
        text: ANSWER,
        messageId: expect.any(String),
        sourceRole: 'assistant',
      });
      // The scaffolding is NOT in the content — that is what keeps a re-send
      // from wrapping it twice.
      expect(asked.content).toBe('why does this happen?');
    });

    // And it is visible on the bubble, as its own block above the question.
    expect(screen.getAllByText('Replying to').length).toBeGreaterThan(0);
  });

  it('REPLY-14 · the pending reference is released once the turn is created', async () => {
    await seedConversation();
    await askAbout(assistantProse(), 'why does this happen?');
    // The composer is clean: no card, no ×, nothing to remove.
    expect(
      screen.queryByRole('button', { name: 'Remove selected context' }),
    ).toBeNull();
  });

  it('REPLY-15 · a refused send keeps the reference for the retry', async () => {
    await seedConversation();
    await selectInside(assistantProse());
    await act(async () => {
      fireEvent.click(askButton()!);
    });
    // An empty box is refused by the composer before `send` is ever called —
    // the reference must survive that, or a mis-click would silently discard
    // an excerpt the user deliberately captured.
    fireEvent.click(sendButton());

    expect(screen.getByText('Replying to')).toBeTruthy();
    expect(chatBodies.length).toBe(0);

    // …and the retry carries it.
    await act(async () => {
      send('now with a question');
      await new Promise((r) => setTimeout(r, 0));
    });
    await waitFor(() => expect(chatBodies.length).toBe(1));
    expect(chatBodies[0].current_text).toContain('Selected context from');
  });

  it('REPLY-24 · re-sending the stored turn does not duplicate the quote', async () => {
    await seedConversation();
    await askAbout(assistantProse(), 'why does this happen?');
    await waitFor(() => expect(chatBodies.length).toBe(1));
    // Regenerate refuses while a stream is open, so wait for the second answer
    // to be on screen before asking for a third.
    await waitFor(() =>
      expect(
        document.querySelectorAll('[data-chat-message-role="assistant"]').length,
      ).toBe(2),
    );

    // Regenerate replays the SAME stored user turn through the same folding.
    const before = chatBodies[0].current_text!;
    const retry = screen.getAllByRole('button', { name: /Try again/i });
    await act(async () => {
      fireEvent.click(retry[retry.length - 1]);
      await new Promise((r) => setTimeout(r, 40));
    });

    await waitFor(() => expect(chatBodies.length).toBe(2));
    const again = chatBodies[1].messages!.filter((m) => m.role === 'user').pop()!;
    // Once, not twice — the wrapper is built from meta at request time and
    // was never written into the stored `content`.
    expect(again.content.match(/Selected context from/g)).toHaveLength(1);
    expect(again.content).toBe(before);
  });
});

/* ================================================= leaving a conversation */

describe('REPLY · a reference belongs to one conversation', () => {
  it('REPLY-17 · New chat clears a pending reference', async () => {
    await seedConversation();
    await selectInside(assistantProse());
    await act(async () => {
      fireEvent.click(askButton()!);
    });
    expect(screen.getByText('Replying to')).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getAllByRole('button', { name: /New chat/i })[0]);
    });
    expect(screen.queryByText('Replying to')).toBeNull();
  });

  it('REPLY-16 · a pending reference never survives a conversation change', async () => {
    // Same guarantee as above, stated against the mechanism rather than one
    // button: leaving the conversation is what ends the reference, whichever
    // route got you out of it.
    await seedConversation();
    await selectInside(assistantProse());
    await act(async () => {
      fireEvent.click(askButton()!);
    });
    await act(async () => {
      fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true, shiftKey: true });
    });
    expect(screen.queryByText('Replying to')).toBeNull();
  });
});
