// @vitest-environment jsdom
/**
 * The split between the conversation and the file panel is measured against
 * the WORKSPACE — the row to the right of the sidebar — never the shell.
 *
 * Measured against the shell (the first build), the minimums were
 * 45 % + 50 % + 6 px of a width that also held the 260 px sidebar: 0.95·W +
 * 266 px, wider than every real screen, and the shell's overflow-hidden
 * clipped the difference off the panel's right edge — the Close and Download
 * controls and the zoom buttons. jsdom has no layout, so this pins the DOM
 * shape the CSS percentages resolve against: the conversation, the divider
 * and the panel share one flex box that excludes the sidebar, and the panel
 * column may yield (no `shrink-0`) rather than be clipped.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ArtifactRef, ChatMessage, Conversation, ConversationSummary } from '@/lib/types';

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const JOB = 'ffffffffffffffffffffffffffffffff';

const ref: ArtifactRef = {
  artifact_id: ID,
  version: 1,
  job_id: JOB,
  title: 'Board deck',
  kind: 'presentation',
  status: 'completed',
  files: [
    {
      format: 'pptx',
      filename: 'board-deck-v1.pptx',
      mime_type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      size: 250_000,
      slides: 9,
      download_url: `/artifacts/${ID}/v/1/file/pptx?disposition=attachment`,
      inline_url: `/artifacts/${ID}/v/1/file/pptx?disposition=inline`,
    },
  ],
  preview_kind: 'none',
  preview_pages: 0,
  preview_url: '',
  thumbnail_url: '',
  warnings: [],
  created_at: '2026-09-11T10:00:00Z',
  operation: 'create',
  status_url: `/artifacts/jobs/${JOB}`,
};

const answer: ChatMessage = {
  id: 'a1',
  role: 'assistant',
  content: 'Here is the deck.',
  status: 'done',
  createdAt: 0,
  meta: { route: 'artifact', artifacts: [ref] },
};

const summaries: ConversationSummary[] = [{ id: 'a', title: 'Chat A', createdAt: 1, updatedAt: 1 }];
const conversation: Conversation = {
  id: 'a',
  title: 'Chat A',
  messages: [answer],
  createdAt: 0,
  updatedAt: 0,
};

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
    get: (id: string) => (id === 'a' ? conversation : null),
    load: async (id: string) => (id === 'a' ? conversation : null),
    create: () => ({ id: 'new', title: 'New chat', messages: [], createdAt: 0, updatedAt: 0 }),
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
  HTMLMediaElement.prototype.play = async () => undefined;
  HTMLMediaElement.prototype.pause = () => undefined;
  // The panel fetches its version on open; a terminal ref keeps it from polling.
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => (typeof url === 'string' && url.endsWith('/v/1') ? ref : {}),
    })),
  );
  window.history.replaceState({}, '', '/');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function openDeck() {
  await act(async () => {
    render(<ChatApp />);
  });
  const rows = await screen.findAllByRole('button', { name: 'Chat A' });
  await act(async () => {
    fireEvent.click(rows[0]);
  });
  const card = await screen.findByRole('button', { name: /Open Board deck/ });
  await act(async () => {
    fireEvent.click(card);
  });
  await act(async () => {
    await Promise.resolve();
  });
}

describe('the file panel splits the workspace, not the shell', () => {
  it('shares one flex box with the conversation and the divider, which excludes the sidebar', async () => {
    await openDeck();

    const workspace = screen.getByTestId('workspace');
    const conversationColumn = document.querySelector('[data-file-drop-zone]');
    const divider = screen.getByRole('separator', { name: 'Resize the file panel' });
    const panelColumn = screen.getByTestId('artifact-panel-column');
    const sidebar = document.querySelector('aside[aria-label="Sidebar"]');

    // The three siblings the percentages are shares of …
    expect(conversationColumn?.parentElement).toBe(workspace);
    expect(divider.parentElement).toBe(workspace);
    expect(panelColumn.parentElement).toBe(workspace);
    // … and the sidebar is outside that box, so its 260 px never enter the sum.
    expect(sidebar).not.toBeNull();
    expect(workspace.contains(sidebar)).toBe(false);
    expect(sidebar!.parentElement).toBe(workspace.parentElement);

    // The workspace fills what the sidebar leaves and may shrink below its content.
    expect(workspace.className).toMatch(/\bflex-1\b/);
    expect(workspace.className).toMatch(/\bmin-w-0\b/);
  });

  it('gives the panel a flex-basis it may yield from, never a fixed width it is clipped at', async () => {
    await openDeck();
    const panelColumn = screen.getByTestId('artifact-panel-column');
    expect(panelColumn.style.flexBasis).toBe('50%');
    expect(panelColumn.style.width).toBe('');
    expect(panelColumn.className).not.toMatch(/\bshrink-0\b/);
    expect(panelColumn.className).toMatch(/min-\[900px\]:min-w-0/);
    // The thread keeps its 45 % of the SAME box.
    const conversationColumn = document.querySelector('[data-file-drop-zone]')!;
    expect(conversationColumn.className).toMatch(/min-\[900px\]:min-w-\[45%\]/);
  });

  it('closes from the panel and the shell is back to sidebar + workspace', async () => {
    await openDeck();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Close preview' }));
    });
    expect(screen.queryByTestId('artifact-panel-column')).toBeNull();
    expect(screen.queryByRole('separator', { name: 'Resize the file panel' })).toBeNull();
    expect(screen.getByTestId('workspace').contains(document.querySelector('[data-file-drop-zone]'))).toBe(true);
  });
});
