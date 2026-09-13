// @vitest-environment jsdom
/**
 * The product name in the chat header must be one string on the server and in
 * the browser (fe re-audit 2026-09-13, regression).
 *
 * ChatApp is a client component. It used to read NEXT_PUBLIC_APP_NAME itself,
 * so its browser bundle carried the value inlined at BUILD time while the
 * server — which renders / per request since app/layout.tsx reads headers()
 * for the CSP nonce — used the RUNTIME value. On the e2e stack ("TechSara AI
 * (e2e)") that was React error #418 on every chat load, and the root
 * re-render wiped the theme class off <html>, so light-theme readers got dark.
 *
 * The fix hands the name down from the server page as a prop. What these
 * tests model: the environment ChatApp's module sees at import stands for the
 * build-time inlining, and it must NOT be what the header says.
 */

import { cleanup, render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.hoisted(() => {
  // What a client bundle built without the runtime value would carry.
  process.env.NEXT_PUBLIC_APP_NAME = 'Name inlined at build';
});

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
    create: (title: string) => ({ id: 'conv-1', title, messages: [], createdAt: 0, updatedAt: 0 }),
    saveMessages: () => undefined,
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
const { default: ChatPage } = await import('@/app/page');

beforeEach(() => {
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
    vi.fn(async (url: string) =>
      String(url) === '/api/chat/active'
        ? { ok: true, status: 200, json: async () => ({ active: [] }) }
        : { ok: true, status: 200, json: async () => ({}) },
    ),
  );
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe('the product name in the chat header', () => {
  it('is the name the server page passes in, not the one the client bundle was built with', async () => {
    render(
      <Providers>
        <ChatApp appName="TechSara AI (e2e)" />
      </Providers>,
    );

    const heading = await screen.findByRole('heading', { level: 1, name: 'TechSara AI (e2e)' });
    expect(heading.className).toContain('sr-only');
    expect(screen.queryByText('Name inlined at build')).toBeNull();
  });

  it('falls back to TechSara AI when a caller passes no name', async () => {
    render(
      <Providers>
        <ChatApp />
      </Providers>,
    );

    await screen.findByRole('heading', { level: 1, name: 'TechSara AI' });
  });

  it('is read by the chat page from the environment at request time', () => {
    vi.stubEnv('NEXT_PUBLIC_APP_NAME', 'TechSara AI (e2e)');
    const element = ChatPage() as ReactElement<{ appName?: string }>;
    expect(element.type).toBe(ChatApp);
    expect(element.props.appName).toBe('TechSara AI (e2e)');

    vi.stubEnv('NEXT_PUBLIC_APP_NAME', undefined);
    expect((ChatPage() as ReactElement<{ appName?: string }>).props.appName).toBe('TechSara AI');
  });
});
