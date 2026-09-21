// @vitest-environment jsdom
/**
 * QA round 2 (security lens) for B11: what must NOT change once the memory
 * dialogs install a window-level key guard, and what a hostile or careless
 * sequence of clicks must never reach.
 */

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GET } from '@/app/api/memory/[...path]/route';
import { MemoryDialog, MemoryPanel } from '@/components/MemoryPanel';
import { MessageRow } from '@/components/MessageRow';
import { Providers } from '@/components/Providers';
import { SettingsDialog } from '@/components/SettingsDialog';
import type { FetchLike } from '@/lib/auth';
import type { MemoryFact } from '@/lib/memory';
import { shortcutAction } from '@/lib/searchPalette';
import type { ChatMessage } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});
beforeEach(() => {
  HTMLMediaElement.prototype.play =
    HTMLMediaElement.prototype.play ?? (async () => undefined);
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const FACTS: MemoryFact[] = [
  {
    id: 11,
    fact: 'Works as a data engineer in Pune',
    source: 'stated',
    source_excerpt: 'I work as a data engineer in Pune',
    created_at: '2026-09-18T09:30:00Z',
    updated_at: '2026-09-18T09:30:00Z',
  },
  {
    id: 12,
    fact: 'Prefers answers in metric units',
    source: 'manual',
    source_excerpt: null,
    created_at: '2026-09-10T12:00:00Z',
    updated_at: '2026-09-10T12:00:00Z',
  },
];

function server(opts: { holdDeletes?: boolean } = {}) {
  const calls: { url: string; method: string }[] = [];
  const release: Array<() => void> = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? 'GET').toUpperCase();
    calls.push({ url, method });
    if (method === 'GET') return json({ facts: FACTS });
    if (opts.holdDeletes) {
      await new Promise<void>((r) => release.push(r));
    }
    return json({ deleted: 1 });
  });
  return { fn, calls, release };
}

/** ChatApp's real window shortcut map with an answer streaming. */
function pageBehind() {
  const actions: string[] = [];
  const documentEscapes: string[] = [];
  function onWindowKey(e: KeyboardEvent) {
    const target = e.target as HTMLElement | null;
    const action = shortcutAction(e, {
      paletteOpen: false,
      quoteActionOpen: false,
      streaming: true,
      typing: target?.tagName === 'INPUT' || target?.tagName === 'TEXTAREA',
    });
    if (action) actions.push(action);
  }
  // ArtifactPanel's own rule (components/artifacts/ArtifactPanel.tsx): skip a
  // key something closer already answered, or one aimed at a dialog.
  function onDocumentKey(e: KeyboardEvent) {
    if (e.key !== 'Escape' || e.defaultPrevented) return;
    const target = e.target instanceof Element ? e.target : null;
    if (target?.closest('[role="dialog"]')) return;
    documentEscapes.push('artifact-panel-closed');
  }
  window.addEventListener('keydown', onWindowKey);
  document.addEventListener('keydown', onDocumentKey);
  return {
    actions,
    documentEscapes,
    remove() {
      window.removeEventListener('keydown', onWindowKey);
      document.removeEventListener('keydown', onDocumentKey);
    },
  };
}

function chipMessage(): ChatMessage {
  return {
    id: 'a1',
    role: 'assistant',
    content: 'Noted.',
    status: 'done',
    createdAt: 0,
    meta: { memory_updated: ['Works as a data engineer in Pune'] },
  };
}

function toBody() {
  act(() => {
    (document.activeElement as HTMLElement | null)?.blur();
  });
  expect(document.activeElement).toBe(document.body);
}

describe('QA2: the page shortcuts are untouched whenever no memory dialog is open', () => {
  let behind: ReturnType<typeof pageBehind>;
  beforeEach(() => {
    behind = pageBehind();
  });
  afterEach(() => behind.remove());

  it('a chip that was never opened leaves Escape (stop) and "/" to the page', () => {
    const { fn } = server();
    vi.stubGlobal('fetch', fn);
    render(
      <Providers>
        <MessageRow message={chipMessage()} isLast onRegenerate={vi.fn()} onRetry={vi.fn()} />
      </Providers>,
    );
    fireEvent.keyDown(document.body, { key: 'Escape' });
    fireEvent.keyDown(document.body, { key: '/' });
    expect(behind.actions).toEqual(['stop-streaming', 'focus-composer']);
    expect(fn).not.toHaveBeenCalled();
  });

  it('after the chip dialog closes, Escape stops the answer again', async () => {
    const { fn } = server();
    vi.stubGlobal('fetch', fn);
    render(
      <Providers>
        <MessageRow message={chipMessage()} isLast onRegenerate={vi.fn()} onRetry={vi.fn()} />
      </Providers>,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Memory updated/ }));
    });
    const dialog = await screen.findByRole('dialog', { name: 'Memory' });
    await waitFor(() => expect(within(dialog).getAllByTestId('memory-fact')).toHaveLength(2));
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(behind.actions).toEqual([]);

    toBody();
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(behind.actions).toEqual(['stop-streaming']);
  });

  it('a row unmounted while its dialog is open leaves no guard behind', async () => {
    const { fn } = server();
    vi.stubGlobal('fetch', fn);
    const view = render(
      <Providers>
        <MessageRow message={chipMessage()} isLast onRegenerate={vi.fn()} onRetry={vi.fn()} />
      </Providers>,
    );
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Memory updated/ }));
    });
    await screen.findByRole('dialog', { name: 'Memory' });
    view.unmount();
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(behind.actions).toEqual(['stop-streaming']);
  });

  it('a closed SettingsDialog installs no guard', () => {
    render(
      <Providers>
        <SettingsDialog open={false} account={null} onClose={vi.fn()} fetchFn={vi.fn() as unknown as FetchLike} />
      </Providers>,
    );
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(behind.actions).toEqual(['stop-streaming']);
  });
});

describe('QA2: nothing is deleted that the person has not been shown', () => {
  it('when the list fails to load there is no "Delete all" to press', async () => {
    const calls: string[] = [];
    const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      calls.push(`${init.method ?? 'GET'} ${String(input)}`);
      return json({ detail: 'boom' }, 500);
    });
    render(
      <Providers>
        <MemoryPanel fetchFn={fn as unknown as FetchLike} />
      </Providers>,
    );
    await screen.findByRole('alert');
    expect(screen.queryByRole('button', { name: 'Delete all' })).toBeNull();
    expect(screen.queryAllByRole('button', { name: /^Delete “/ })).toHaveLength(0);
    expect(calls.every((c) => c.startsWith('GET '))).toBe(true);
  });

  it('while one delete is in flight every other delete control is disabled', async () => {
    const { fn, calls, release } = server({ holdDeletes: true });
    render(
      <Providers>
        <MemoryPanel fetchFn={fn as unknown as FetchLike} />
      </Providers>,
    );
    await waitFor(() => expect(screen.getAllByTestId('memory-fact')).toHaveLength(2));
    fireEvent.click(screen.getByRole('button', { name: /^Delete “Works/ }));
    const confirm = await screen.findByRole('alertdialog');
    await act(async () => {
      fireEvent.click(within(confirm).getByRole('button', { name: 'Delete' }));
    });
    const other = screen.getByRole('button', { name: /^Delete “Prefers/ }) as HTMLButtonElement;
    const all = screen.getByRole('button', { name: 'Delete all' }) as HTMLButtonElement;
    expect(other.disabled).toBe(true);
    expect(all.disabled).toBe(true);
    fireEvent.click(all);
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(calls.filter((c) => c.method === 'DELETE')).toEqual([
      { url: '/api/memory/facts/11', method: 'DELETE' },
    ]);
    await act(async () => {
      release.forEach((r) => r());
    });
  });
});

describe('QA2: an Escape answered by a confirm on top of the memory dialog', () => {
  let behind: ReturnType<typeof pageBehind>;
  beforeEach(() => {
    behind = pageBehind();
  });
  afterEach(() => behind.remove());

  it('closes only the confirm, never a side panel behind the stack', async () => {
    const { fn } = server();
    render(
      <Providers>
        <MemoryDialog open onClose={vi.fn()} fetchFn={fn as unknown as FetchLike} />
      </Providers>,
    );
    await waitFor(() => expect(screen.getAllByTestId('memory-fact')).toHaveLength(2));
    fireEvent.click(screen.getByRole('button', { name: /^Delete “Works/ }));
    await screen.findByRole('alertdialog');
    // A click on the confirm's own text: its panel cannot take focus.
    toBody();
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(screen.getByRole('dialog', { name: 'Memory' })).toBeTruthy();
    expect(behind.actions).toEqual([]);
    expect(behind.documentEscapes).toEqual([]);
  });
});

describe('QA2: Escape in the Settings > Sessions confirm, focus on its Cancel (the default)', () => {
  let behind: ReturnType<typeof pageBehind>;
  beforeEach(() => {
    behind = pageBehind();
  });
  afterEach(() => behind.remove());

  it('closes only the confirm; a side panel behind Settings stays open', async () => {
    const fn = vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/auth/sessions') {
        return json({
          sessions: [
            { id: 's1', current: true, created_at: '2026-09-18T09:00:00Z', last_seen_at: '2026-09-18T09:00:00Z', user_agent: null },
            { id: 's2', current: false, created_at: '2026-09-17T09:00:00Z', last_seen_at: '2026-09-17T09:00:00Z', user_agent: null },
          ],
        });
      }
      return json({ detail: 'unexpected' }, 599);
    });
    const onClose = vi.fn();
    render(
      <Providers>
        <SettingsDialog open initialSection="sessions" account={null} onClose={onClose} fetchFn={fn as unknown as FetchLike} />
      </Providers>,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    const confirm = await screen.findByRole('alertdialog');
    const cancel = within(confirm).getByRole('button', { name: 'Cancel' });
    expect(document.activeElement).toBe(cancel);
    fireEvent.keyDown(cancel, { key: 'Escape' });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(behind.actions).toEqual([]);
    expect(behind.documentEscapes).toEqual([]);
  });
});

describe('QA2: proxy', () => {
  it('HEAD (which Next routes to the GET handler) is a 404 that never leaves', async () => {
    vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
    vi.stubEnv('MOCK_MODE', 'false');
    const calls: string[] = [];
    vi.stubGlobal('fetch', async (url: string) => {
      calls.push(String(url));
      return json({ facts: [] });
    });
    const res = await GET(
      new Request('http://localhost:3001/api/memory/facts', {
        method: 'HEAD',
        headers: { cookie: 'ts_session=a' },
      }),
      { params: Promise.resolve({ path: ['facts'] }) },
    );
    expect(res.status).toBe(404);
    expect(calls).toHaveLength(0);
  });
});
