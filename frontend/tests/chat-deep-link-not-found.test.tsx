// @vitest-environment jsdom
/**
 * A ?c= deep link to a conversation the server does not have (fe audit
 * 2026-09-13, owner request: responsive and free of errors).
 *
 * `/?c=<deleted or unknown id>` used to open a silent New Chat under the bad
 * id: the id stayed in the address bar (so a reload did it all again), the
 * conversation was fetched twice — once by the mount effect and once by the
 * poll's first tick, before the cache had even hydrated — and the browser
 * logged two 404s. Nobody was told the chat was not found.
 *
 * The store is a double; what it models is the one fact the real store now
 * reports (`wasNotFound`, pinned in tests/history-server.test.ts): the server
 * answered 404 for an id this browser never cached.
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
import { PREFS_STORAGE_KEY } from '@/lib/prefs';

/** Ids the "server" has answered 404 for. */
const missing = new Set<string>();
let loadCalls: string[] = [];

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
    saveMessages: () => undefined,
    load: async (id: string) => {
      loadCalls.push(id);
      return null;
    },
    wasNotFound: (id: string) => missing.has(id),
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
  handleSessionEnd: async () => undefined,
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

let fetchUrls: string[] = [];

function stubEnv() {
  fetchUrls = [];
  loadCalls = [];
  missing.clear();
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
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      fetchUrls.push(String(url));
      if (String(url) === '/api/chat/active') {
        return { ok: true, status: 200, json: async () => ({ active: [] }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    }),
  );
}

/** A conversation's stored composer prefs, as an earlier visit left them. */
function storeThinkPrefs(id: string) {
  window.localStorage.setItem(
    PREFS_STORAGE_KEY,
    JSON.stringify({
      [id]: {
        salesforce: true,
        sfLive: false,
        model: 'smart',
        effort: 'think',
        agent: false,
        webSearch: 'auto',
        deepResearch: false,
      },
    }),
  );
}

const renderApp = () =>
  render(
    <Providers>
      <ChatApp />
    </Providers>,
  );

/** Let every pending effect and microtask chain run out. */
const settle = () => act(async () => new Promise((r) => setTimeout(r, 50)));

beforeEach(() => {
  stubEnv();
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('a ?c= deep link to a conversation that does not exist', () => {
  it('says the conversation was not found, drops the id from the address bar and asks the server once', async () => {
    missing.add('gone-chat');
    window.history.replaceState(null, '', '/?c=gone-chat');
    renderApp();

    const notice = await screen.findByTestId('conversation-not-found');
    expect(notice.getAttribute('role')).toBe('status');
    expect(notice.textContent).toContain('Conversation not found');
    expect(window.location.search).toBe('');
    expect(window.location.pathname).toBe('/');

    await settle();
    // One read of the conversation and one status question — the poll's
    // first tick no longer repeats the mount effect's work.
    expect(loadCalls).toEqual(['gone-chat']);
    expect(fetchUrls.filter((u) => u === '/api/chat/active')).toHaveLength(1);
    // The greeting of the new chat that is actually on screen.
    expect(screen.getByText('What can I help with?')).toBeTruthy();
  });

  it('opens the new chat from the defaults, not from the missing chat’s stored prefs', async () => {
    missing.add('gone-chat');
    storeThinkPrefs('gone-chat');
    window.history.replaceState(null, '', '/?c=gone-chat');
    renderApp();

    await screen.findByTestId('conversation-not-found');
    expect(screen.getByText('Fast')).toBeTruthy();
    expect(screen.queryByText('Think')).toBeNull();
  });

  it('keeps the chat and its link when the conversation could not be loaded for any other reason', async () => {
    // load() answers null for a network failure too; that is not a verdict.
    storeThinkPrefs('offline-chat');
    window.history.replaceState(null, '', '/?c=offline-chat');
    renderApp();

    await waitFor(() => expect(loadCalls).toContain('offline-chat'));
    await settle();
    expect(screen.queryByTestId('conversation-not-found')).toBeNull();
    expect(window.location.search).toBe('?c=offline-chat');
    expect(screen.getByText('Think')).toBeTruthy();
  });

  it('goes away when dismissed, and when the person starts a new chat', async () => {
    missing.add('gone-chat');
    window.history.replaceState(null, '', '/?c=gone-chat');
    const first = renderApp();

    await screen.findByTestId('conversation-not-found');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Dismiss notice' }));
    });
    expect(screen.queryByTestId('conversation-not-found')).toBeNull();
    first.unmount();

    window.history.replaceState(null, '', '/?c=gone-chat');
    loadCalls = [];
    renderApp();
    await screen.findByTestId('conversation-not-found');
    await act(async () => {
      fireEvent.click(screen.getAllByRole('button', { name: /New chat/ })[0]);
    });
    expect(screen.queryByTestId('conversation-not-found')).toBeNull();
  });
});
