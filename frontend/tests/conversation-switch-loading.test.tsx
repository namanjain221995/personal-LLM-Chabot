// @vitest-environment jsdom
/**
 * M-10: what the thread column shows while a conversation's history is in
 * flight.
 *
 * Selecting a chat this browser has never opened had two wrong outcomes,
 * depending on whether the cache had heard of it at all:
 *
 *   - no cache entry     → setMessages was never called, so the PREVIOUS
 *                          conversation's messages stayed on screen under the
 *                          new chat's id, title and URL;
 *   - a listed-but-empty → setMessages([]) rendered the New Chat greeting, so
 *     cache entry          a conversation with history claimed to be blank.
 *
 * The first is the serious one: never show conversation A's messages under
 * conversation B's identity. These tests pin that, the honest third state that
 * replaces both, and the stale-response guard that makes rapid switching safe.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ChatMessage, Conversation, ConversationSummary } from '@/lib/types';

/* ------------------------------------------------------------- fixtures */

const msg = (id: string, content: string): ChatMessage => ({
  id,
  role: 'assistant',
  content,
  status: 'done',
  createdAt: 0,
});

const A_TEXT = 'ALPHA answer from chat A';
const B_TEXT = 'BRAVO answer from chat B';
const C_TEXT = 'CHARLIE answer from chat C';

const summaries: ConversationSummary[] = [
  { id: 'a', title: 'Chat A', createdAt: 3, updatedAt: 3 },
  { id: 'b', title: 'Chat B', createdAt: 2, updatedAt: 2 },
  { id: 'c', title: 'Chat C', createdAt: 1, updatedAt: 1 },
];

const conv = (id: string, messages: ChatMessage[]): Conversation => ({
  id,
  title: `Chat ${id.toUpperCase()}`,
  messages,
  createdAt: 0,
  updatedAt: 0,
});

/* ------------------------------------------- a store we can hold open */

/** Cache entries by id. `undefined` = the store has never heard of it. */
let cache: Record<string, Conversation | undefined>;
/** Pending load() resolvers, so a test decides exactly when history lands. */
let pending: Record<string, (c: Conversation | null) => void>;
let loadCalls: string[];

function deferLoad(id: string): Promise<Conversation | null> {
  loadCalls.push(id);
  return new Promise((resolve) => {
    pending[id] = resolve;
  });
}

/** Resolve a held-open load with the given conversation (or null = failure). */
async function land(id: string, c: Conversation | null) {
  await act(async () => {
    pending[id]?.(c);
    await Promise.resolve();
  });
}

vi.mock('@/lib/history', () => ({
  newId: () => `m${Math.random().toString(36).slice(2, 10)}`,
  setEvictListener: () => undefined,
  rebuildHistoryStore: async () => {
    throw new Error('unexpected account switch in test');
  },
  getHistoryStore: () => ({
    ready: async () => undefined,
    list: () => summaries,
    listArchived: () => [],
    get: (id: string) => cache[id] ?? null,
    load: (id: string) => deferLoad(id),
    create: () => conv('new', []),
    saveMessages: () => undefined,
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
  fetchMe: async () => ({ ok: true, username: 'tester', user: null, features: {} }),
  userScopeKey: () => 'tester',
  redirectToLogin: () => undefined,
  handleSessionEnd: () => undefined,
  isAccessEnded: () => false,
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

import { ChatApp } from '../components/ChatApp';

/* ---------------------------------------------------------------- setup */

beforeEach(() => {
  cache = {};
  pending = {};
  loadCalls = [];
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
  HTMLMediaElement.prototype.play = async () => undefined;
  HTMLMediaElement.prototype.pause = () => undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })),
  );
  window.history.replaceState({}, '', '/');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function mount() {
  await act(async () => {
    render(<ChatApp />);
  });
}

/** The sidebar is rendered twice (desktop rail + mobile drawer); either row
 *  calls the same onSelect, so the first match is the one to click. */
const open = async (title: string) => {
  const rows = await screen.findAllByRole('button', { name: title });
  await act(async () => {
    fireEvent.click(rows[0]);
  });
};

const loading = () => screen.queryByTestId('conversation-loading');
const greeting = () => screen.queryByText('What can I help with?');
const onScreen = (text: string) => screen.queryByText(text) !== null;

/* ----------------------------------------------------------------- tests */

describe('M-10 · CASE 1 — the previous chat never shows under the new one', () => {
  it('drops A the instant B is selected, even with no cache entry for B', async () => {
    cache = { a: conv('a', [msg('a1', A_TEXT)]) };
    await mount();

    await open('Chat A');
    expect(onScreen(A_TEXT)).toBe(true);

    await open('Chat B');

    // The heart of the bug. A's answer must be gone the moment B is active.
    expect(onScreen(A_TEXT)).toBe(false);
  });

  it('drops A when B IS cached but has no messages yet (listed, never opened)', async () => {
    cache = { a: conv('a', [msg('a1', A_TEXT)]), b: conv('b', []) };
    await mount();

    await open('Chat A');
    expect(onScreen(A_TEXT)).toBe(true);

    await open('Chat B');
    expect(onScreen(A_TEXT)).toBe(false);
  });
});

describe('M-10 · CASE 2 — a loading chat is not a new chat', () => {
  it('shows the loading state, not the New Chat greeting, for an uncached chat', async () => {
    await mount();
    await open('Chat B');

    expect(loading()).not.toBeNull();
    expect(greeting()).toBeNull();
  });

  it('says the same for a listed-but-empty cache entry', async () => {
    cache = { b: conv('b', []) };
    await mount();
    await open('Chat B');

    expect(loading()).not.toBeNull();
    expect(greeting()).toBeNull();
  });

  it('a genuinely NEW chat still shows the greeting, not a spinner', async () => {
    cache = { a: conv('a', [msg('a1', A_TEXT)]) };
    await mount();
    await open('Chat A');

    await act(async () => {
      fireEvent.click(screen.getAllByRole('button', { name: /New chat/i })[0]);
    });

    expect(greeting()).not.toBeNull();
    expect(loading()).toBeNull();
  });
});

describe('M-10 · CASE 3 — history arrives', () => {
  it('renders B once its load resolves, and stops loading', async () => {
    await mount();
    await open('Chat B');
    expect(loading()).not.toBeNull();

    await land('b', conv('b', [msg('b1', B_TEXT)]));

    await waitFor(() => expect(onScreen(B_TEXT)).toBe(true));
    expect(loading()).toBeNull();
    expect(greeting()).toBeNull();
  });
});

describe('M-10 · CASE 4 — the latest selection wins', () => {
  it('a late response for A cannot replace B', async () => {
    await mount();

    await open('Chat A');
    await open('Chat B');

    // A resolves AFTER the user moved on. It must not paint.
    await land('a', conv('a', [msg('a1', A_TEXT)]));
    expect(onScreen(A_TEXT)).toBe(false);

    // ...and it must not have stolen B's loading state either.
    expect(loading()).not.toBeNull();

    await land('b', conv('b', [msg('b1', B_TEXT)]));
    await waitFor(() => expect(onScreen(B_TEXT)).toBe(true));
    expect(onScreen(A_TEXT)).toBe(false);
  });

  it('A → B → C: only C is ever shown, whatever order the loads land in', async () => {
    await mount();

    await open('Chat A');
    await open('Chat B');
    await open('Chat C');

    await land('b', conv('b', [msg('b1', B_TEXT)]));
    await land('a', conv('a', [msg('a1', A_TEXT)]));
    await land('c', conv('c', [msg('c1', C_TEXT)]));

    await waitFor(() => expect(onScreen(C_TEXT)).toBe(true));
    expect(onScreen(A_TEXT)).toBe(false);
    expect(onScreen(B_TEXT)).toBe(false);
  });
});

describe('M-10 · CASE 5 — a failed load', () => {
  it('never falls back to A under B, and does not spin for ever', async () => {
    cache = { a: conv('a', [msg('a1', A_TEXT)]) };
    await mount();

    await open('Chat A');
    await open('Chat B');
    expect(loading()).not.toBeNull();

    // The store could not produce B (offline with nothing cached).
    await land('b', null);

    await waitFor(() => expect(loading()).toBeNull());
    // The failure must not resurrect A under B's identity.
    expect(onScreen(A_TEXT)).toBe(false);
  });
});

describe('M-10 · CASE 6 — a cached conversation is instant', () => {
  it('paints from cache with no loading state at all', async () => {
    cache = { b: conv('b', [msg('b1', B_TEXT)]) };
    await mount();

    await open('Chat B');

    expect(onScreen(B_TEXT)).toBe(true);
    expect(loading()).toBeNull();
    expect(greeting()).toBeNull();
  });

  it('switching back to a chat cached by an earlier load is instant too', async () => {
    cache = { a: conv('a', [msg('a1', A_TEXT)]) };
    await mount();

    await open('Chat B');
    // The store caches what it loaded, as the real one does.
    cache.b = conv('b', [msg('b1', B_TEXT)]);
    await land('b', cache.b);
    await waitFor(() => expect(onScreen(B_TEXT)).toBe(true));

    await open('Chat A');
    expect(onScreen(A_TEXT)).toBe(true);
    expect(loading()).toBeNull();

    await open('Chat B');
    expect(onScreen(B_TEXT)).toBe(true);
    expect(loading()).toBeNull();
  });
});
