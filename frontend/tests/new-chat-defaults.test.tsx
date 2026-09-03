// @vitest-environment jsdom
/**
 * What a NEW chat starts as, and what it therefore SENDS (owner request
 * 2026-09-03).
 *
 * Every new chat used to open in Salesforce mode at the Think level, so an
 * ordinary question had to be un-scoped and un-slowed before it could be
 * asked. Both are still one click away; neither is the starting position any
 * more.
 *
 * The load-bearing half of this file is the REQUEST. A test that only read the
 * composer would pass just as happily for a build that showed "Fast" while
 * posting `effort: "think"` — which is precisely the failure the owner called
 * out ("must NOT visually say Fast while still sending mode=think"). So the
 * real ChatApp drives the real startStream and the body is captured off the
 * wire, with the UI asserted against the same run.
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
import { DEFAULT_PREFS, PREFS_STORAGE_KEY } from '@/lib/prefs';
import type { ChatRequestBody } from '@/lib/orchestrator';


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

/** One `done` event, so the real consume() finishes cleanly. */
function sseBody(): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(new TextEncoder().encode('event: done\ndata: {}\n\n'));
      c.close();
    },
  });
}

let chatBodies: ChatRequestBody[] = [];

function stubEnv() {
  chatBodies = [];
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
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).startsWith('/api/chat')) {
        chatBodies.push(JSON.parse(String(init?.body)) as ChatRequestBody);
        return { ok: true, status: 200, body: sseBody() };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    }),
  );
}

const renderApp = () =>
  render(
    <Providers>
      <ChatApp />
    </Providers>,
  );

const box = () => screen.getByRole('textbox', { name: 'Message' });

async function ask(text: string) {
  await act(async () => {
    fireEvent.change(box(), { target: { value: text } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await new Promise((r) => setTimeout(r, 0));
  });
  await waitFor(() => expect(chatBodies.length).toBeGreaterThan(0));
  return chatBodies[chatBodies.length - 1];
}

/**
 * Open the composer "+" menu and read the rows the user actually sees.
 *
 * Read through `aria-checked` on the real `menuitemcheckbox` rows rather than
 * through prefs: that attribute IS what a screen reader and the tick mark both
 * report, so it is the same fact the owner checks by looking.
 */
async function openMenu() {
  // The trigger TOGGLES, and activating a row leaves the popover closed, so a
  // blind click is only right half the time. Open it if it is shut, and wait
  // for the rows rather than assuming the state flipped synchronously.
  if (screen.queryAllByRole('menuitemcheckbox').length > 0) return;
  await act(async () => {
    fireEvent.click(
      screen.getByRole('button', { name: 'Add photos, files and tools' }),
    );
  });
  await screen.findAllByRole('menuitemcheckbox');
}

/** Activating a row closes the popover, so each read re-opens it. */
async function menuState(): Promise<Record<string, string | null>> {
  await openMenu();
  const rows = screen.getAllByRole('menuitemcheckbox');
  const out: Record<string, string | null> = {};
  for (const r of rows) {
    const label = (r.textContent ?? '').trim();
    for (const key of ['Web search', 'Deep research', 'Live Salesforce', 'Salesforce']) {
      if (label.startsWith(key) && !(key in out)) out[key] = r.getAttribute('aria-checked');
    }
  }
  return out;
}

async function clickMenuRow(startsWith: string) {
  await openMenu();
  const row = screen
    .getAllByRole('menuitemcheckbox')
    .find((r) => (r.textContent ?? '').trim().startsWith(startsWith));
  expect(row, `no menu row starting "${startsWith}"`).toBeTruthy();
  await act(async () => {
    fireEvent.click(row as HTMLElement);
  });
}

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

/* ======================================================= the visible state */

describe('DEFAULT · what a brand-new chat looks like', () => {
  it('DEFAULT-01/02 · the level picker reads Fast, not Think', async () => {
    renderApp();
    // The picker's trigger shows the active level.
    expect(screen.getByText('Fast')).toBeTruthy();
    expect(screen.queryByText('Think')).toBeNull();
  });

  it('DEFAULT-03/04/05/06 · nothing optional is ticked in the "+" menu', async () => {
    renderApp();
    expect(await menuState()).toEqual({
      'Web search': 'false',
      'Deep research': 'false',
      Salesforce: 'false',
      'Live Salesforce': 'false',
    });
  });

  it('the trust line says Salesforce is off rather than promising a warehouse', async () => {
    renderApp();
    expect(screen.getByText(/Salesforce is off/i)).toBeTruthy();
  });
});

/* ============================================== the request that goes out */

describe('DEFAULT · what a brand-new chat SENDS', () => {
  it('DEFAULT-07…11 · Fast, assistant mode, nothing else armed', async () => {
    renderApp();
    const body = await ask('hello');

    expect(body.effort).toBe('fast');
    expect(body.mode).toBe('assistant');
    expect(body.sf_live).toBe(false);
    expect(body.deep_research).toBe(false);
    // 'auto' is the OFF position of the web-search control (the menu ticks it
    // only at 'on'); what must never be true of an untouched chat is 'on'.
    expect(body.web_search).not.toBe('on');
    expect(body.agent).toBe(false);
  });

  it('the UI and the wire agree — the exact failure the owner named', async () => {
    renderApp();
    expect(screen.getByText('Fast')).toBeTruthy();
    const body = await ask('hello');
    expect(body.effort).toBe('fast');
    expect(body.mode).not.toBe('salesforce');
  });
});

/* ================================================ no leak from chat to chat */

describe('DEFAULT · New Chat starts neutral however the last one ended', () => {
  it('a conversation left in Salesforce + Think does not seed the next one', async () => {
    renderApp();

    // Chat A: arm the special modes the way a user does — through the menu,
    // so the app's own exclusivity rules apply rather than a hand-built state.
    await clickMenuRow('Live Salesforce');
    expect(await menuState()).toMatchObject({
      Salesforce: 'true',
      'Live Salesforce': 'true',
    });

    const first = await ask('scoped question');
    expect(first.mode).toBe('salesforce');
    expect(first.sf_live).toBe(true);

    // New Chat.
    chatBodies = [];
    await act(async () => {
      fireEvent.click(screen.getAllByRole('button', { name: /New chat/i })[0]);
    });

    // Visible state is neutral again…
    expect(screen.getByText('Fast')).toBeTruthy();
    expect(await menuState()).toEqual({
      'Web search': 'false',
      'Deep research': 'false',
      Salesforce: 'false',
      'Live Salesforce': 'false',
    });

    // …and so is the wire, which is the half a label check cannot prove.
    const second = await ask('ordinary question');
    expect(second.mode).toBe('assistant');
    expect(second.sf_live).toBe(false);
    expect(second.deep_research).toBe(false);
    expect(second.effort).toBe('fast');
  });

  it('a stale draft slot from an older session cannot resurrect itself', async () => {
    // The subtle one. Nothing renders from the stored draft slot, so a blank
    // app looked neutral either way — but `send()` adopts that slot when it
    // creates the conversation, so an old Salesforce draft would snap the
    // composer back the instant the first message landed.
    window.localStorage.setItem(
      PREFS_STORAGE_KEY,
      JSON.stringify({
        __draft__: { salesforce: true, sfLive: true, effort: 'think', model: 'smart' },
      }),
    );
    renderApp();

    const body = await ask('hello');
    expect(body.mode).toBe('assistant');
    expect(body.effort).toBe('fast');
    // And the composer has not flipped underneath the user after the send.
    await waitFor(() => expect(screen.getByText('Fast')).toBeTruthy());
    expect(screen.getByText(/Salesforce is off/i)).toBeTruthy();
  });

  it('DEFAULT · manual activation still works after the reset', async () => {
    renderApp();
    await clickMenuRow('Salesforce');
    const body = await ask('scoped');
    expect(body.mode).toBe('salesforce');
  });
});

/* ============================================ existing chats are untouched */

describe('DEFAULT · existing conversations keep their own choices', () => {
  it('reopening a stored conversation restores what it was using', async () => {
    // The change is to the NEW-chat default. A conversation that recorded
    // Salesforce + Think must still open that way — nothing was migrated,
    // rewritten or erased.
    window.localStorage.setItem(
      PREFS_STORAGE_KEY,
      JSON.stringify({
        'conv-old': {
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
    window.history.replaceState(null, '', '/?c=conv-old');
    renderApp();

    await waitFor(() => expect(screen.getByText('Think')).toBeTruthy());
    expect(screen.getByText(/synced Salesforce data/i)).toBeTruthy();
    // The stored entry is still exactly what it was.
    const map = JSON.parse(window.localStorage.getItem(PREFS_STORAGE_KEY) as string);
    expect(map['conv-old']).toMatchObject({ salesforce: true, effort: 'think' });
  });

  it('DEFAULT_PREFS is the only default — nothing hard-codes a second one', () => {
    expect(DEFAULT_PREFS.salesforce).toBe(false);
    expect(DEFAULT_PREFS.effort).toBe('fast');
  });
});
