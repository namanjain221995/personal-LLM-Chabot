// @vitest-environment jsdom
/**
 * The memory panel (B11, QA-mem-6): a person can SEE what the assistant saved
 * about them — each fact with where it came from and when — and delete one
 * fact or all of them.
 *
 * Before this existed the orchestrator held 132 production facts written
 * under the old extraction rules (task requests, one jailbreak attempt) and
 * the "Memory updated" chip under an answer opened nothing, so none of them
 * could be seen or removed by the person they describe.
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

import { MemoryDialog, MemoryPanel } from '@/components/MemoryPanel';
import { MessageRow } from '@/components/MessageRow';
import { Providers } from '@/components/Providers';
import { SettingsDialog } from '@/components/SettingsDialog';
import type { FetchLike } from '@/lib/auth';
import { formatDay } from '@/lib/format';
import {
  CLEAR_ALL_PHRASE,
  clipText,
  excerptQuote,
  sourceLabel,
  type MemoryFact,
} from '@/lib/memory';
import { shortcutAction } from '@/lib/searchPalette';
import type { ChatMessage } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
beforeEach(() => {
  // Loader renders a <video>; jsdom's media element needs a play stub.
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
    source_conversation_id: 'c-1',
    source: 'stated',
    source_excerpt: 'I work as a data engineer in Pune and mostly write SQL',
    created_at: '2026-09-18T09:30:00Z',
    updated_at: '2026-09-18T09:30:00Z',
  },
  {
    id: 12,
    fact: 'Prefers answers in metric units',
    source_conversation_id: null,
    source: 'manual',
    source_excerpt: null,
    created_at: '2026-09-10T12:00:00Z',
    updated_at: '2026-09-10T12:00:00Z',
  },
  {
    id: 13,
    fact: 'Wants a cover letter for the Acme role',
    source_conversation_id: 'c-0',
    source: null,
    source_excerpt: null,
    created_at: '2026-08-02T08:00:00Z',
    updated_at: '2026-08-02T08:00:00Z',
  },
];

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;

/** A fetch that records every call and answers from a route table. */
function fakeServer(handler?: Handler) {
  let facts = [...FACTS];
  const calls: { url: string; method: string }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    const method = (init.method ?? 'GET').toUpperCase();
    calls.push({ url, method });
    if (handler) {
      const answer = await handler(url, init);
      if (answer) return answer;
    }
    if (url === '/api/memory/facts' && method === 'GET') {
      return json({ facts });
    }
    if (url === '/api/memory/facts?confirm=all' && method === 'DELETE') {
      const n = facts.length;
      facts = [];
      return json({ deleted: n });
    }
    const one = /^\/api\/memory\/facts\/(\d+)$/.exec(url);
    if (one && method === 'DELETE') {
      const id = Number(one[1]);
      if (!facts.some((f) => f.id === id)) return json({ detail: 'fact not found' }, 404);
      facts = facts.filter((f) => f.id !== id);
      return json({ deleted: id });
    }
    return json({ detail: 'unexpected' }, 599);
  });
  return { fn, calls };
}

function renderPanel(fn: ReturnType<typeof vi.fn>) {
  return render(
    <Providers>
      <MemoryPanel fetchFn={fn as unknown as FetchLike} />
    </Providers>,
  );
}

const rows = () => screen.getAllByTestId('memory-fact');

/* ------------------------------------------------------------------ lib */

describe('lib/memory', () => {
  it('labels each provenance, and an absent one as unknown', () => {
    expect(sourceLabel('stated')).toBe('From your message');
    expect(sourceLabel('manual')).toBe('Added by you');
    expect(sourceLabel(null)).toBe('Origin unknown');
    expect(sourceLabel(undefined)).toBe('Origin unknown');
  });

  it('quotes an excerpt briefly, on one line', () => {
    expect(excerptQuote('  I work\n in Pune ')).toBe('I work in Pune');
    const long = 'word '.repeat(80);
    const quoted = excerptQuote(long)!;
    expect(quoted.length).toBeLessThanOrEqual(141);
    expect(quoted.endsWith('…')).toBe(true);
    expect(excerptQuote(null)).toBeNull();
    expect(excerptQuote('   ')).toBeNull();
  });
});

/* ---------------------------------------------------------------- panel */

describe('MemoryPanel', () => {
  it('shows a loading state, then every fact with its source label, quote and date', async () => {
    const { fn } = fakeServer();
    renderPanel(fn);
    expect(screen.getByLabelText('Loading memory')).toBeTruthy();

    await waitFor(() => expect(rows()).toHaveLength(3));
    const [stated, manual, unknown] = rows();

    expect(within(stated).getByText('Works as a data engineer in Pune')).toBeTruthy();
    expect(within(stated).getByText('From your message')).toBeTruthy();
    // The words behind the fact, quoted, so the person can judge the paraphrase.
    expect(
      within(stated).getByText(/I work as a data engineer in Pune and mostly write SQL/),
    ).toBeTruthy();
    expect(within(stated).getByText(/2026/)).toBeTruthy();

    expect(within(manual).getByText('Added by you')).toBeTruthy();
    expect(within(unknown).getByText('Origin unknown')).toBeTruthy();
    expect(within(unknown).getByText(/2026/)).toBeTruthy();
    expect(fn).toHaveBeenCalledWith('/api/memory/facts', expect.anything());
  });

  it('shows the empty state when nothing is saved', async () => {
    const fn = vi.fn(async () => json({ facts: [] }));
    renderPanel(fn);
    expect(await screen.findByText(/Nothing saved yet/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Delete all/ })).toBeNull();
  });

  it('a 500 shows the error state, and Retry recovers', async () => {
    let fail = true;
    const { fn } = fakeServer(() =>
      fail ? json({ detail: 'boom' }, 500) : (undefined as unknown as Response),
    );
    renderPanel(fn);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/Could not load your memory/);
    expect(screen.queryAllByTestId('memory-fact')).toHaveLength(0);

    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(rows()).toHaveLength(3));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('one delete asks first, calls DELETE with its id, and removes only that row', async () => {
    const { fn, calls } = fakeServer();
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(3));

    fireEvent.click(
      within(rows()[1]).getByRole('button', { name: /Delete/ }),
    );
    const confirm = await screen.findByRole('alertdialog');
    expect(confirm.textContent).toMatch(/Prefers answers in metric units/);
    // Nothing is sent until the person confirms.
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0);

    fireEvent.click(within(confirm).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(rows()).toHaveLength(2));
    expect(calls.filter((c) => c.method === 'DELETE')).toEqual([
      { url: '/api/memory/facts/12', method: 'DELETE' },
    ]);
    expect(screen.queryByText('Prefers answers in metric units')).toBeNull();
    expect(screen.getByText('Works as a data engineer in Pune')).toBeTruthy();
  });

  it('cancelling a delete sends nothing and keeps the row', async () => {
    const { fn, calls } = fakeServer();
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(3));
    fireEvent.click(within(rows()[0]).getByRole('button', { name: /Delete/ }));
    const confirm = await screen.findByRole('alertdialog');
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(rows()).toHaveLength(3);
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0);
  });

  it('a failed delete keeps the row and says so', async () => {
    const { fn } = fakeServer((url, init) =>
      init.method === 'DELETE' ? json({ detail: 'boom' }, 500) : (undefined as unknown as Response),
    );
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(3));
    fireEvent.click(within(rows()[0]).getByRole('button', { name: /Delete/ }));
    fireEvent.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }),
    );
    expect((await screen.findByRole('alert')).textContent).toMatch(/was not deleted/);
    expect(rows()).toHaveLength(3);
  });

  it('clear-all requires the typed confirmation before it sends anything', async () => {
    const { fn, calls } = fakeServer();
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(3));

    fireEvent.click(screen.getByRole('button', { name: /Delete all/ }));
    const dialog = await screen.findByRole('alertdialog', { name: /Delete everything/ });
    const input = within(dialog).getByLabelText(new RegExp(`Type ${CLEAR_ALL_PHRASE}`));
    const confirm = within(dialog).getByRole('button', { name: 'Delete all memory' });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);

    // A near miss is not a confirmation — and neither is Enter on one.
    fireEvent.change(input, { target: { value: 'delete al' } });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.click(confirm);
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0);

    fireEvent.change(input, { target: { value: CLEAR_ALL_PHRASE } });
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(confirm);

    expect(await screen.findByText(/Nothing saved yet/)).toBeTruthy();
    expect(calls.filter((c) => c.method === 'DELETE')).toEqual([
      { url: '/api/memory/facts?confirm=all', method: 'DELETE' },
    ]);
  });

  it('Escape closes the typed confirmation without clearing anything', async () => {
    const { fn, calls } = fakeServer();
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(3));
    fireEvent.click(screen.getByRole('button', { name: /Delete all/ }));
    const dialog = await screen.findByRole('alertdialog', { name: /Delete everything/ });
    const input = within(dialog).getByLabelText(new RegExp(`Type ${CLEAR_ALL_PHRASE}`));
    // Focus starts in the field, so the keyboard path is type-then-Enter.
    expect(document.activeElement).toBe(input);
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(rows()).toHaveLength(3);
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0);
  });

  it('Enter in the typed field confirms once the phrase matches', async () => {
    const { fn, calls } = fakeServer();
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(3));
    fireEvent.click(screen.getByRole('button', { name: /Delete all/ }));
    const dialog = await screen.findByRole('alertdialog', { name: /Delete everything/ });
    const input = within(dialog).getByLabelText(new RegExp(`Type ${CLEAR_ALL_PHRASE}`));
    fireEvent.change(input, { target: { value: CLEAR_ALL_PHRASE } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(await screen.findByText(/Nothing saved yet/)).toBeTruthy();
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(1);
  });

  it('every control is a real button with a name, so the keyboard reaches all of them', async () => {
    const { fn } = fakeServer();
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(3));
    for (const row of rows()) {
      const del = within(row).getByRole('button', { name: /^Delete “/ });
      expect(del.tagName).toBe('BUTTON');
      expect(del.getAttribute('tabindex')).not.toBe('-1');
    }
  });

  it('marks the facts the reply that opened it just saved', async () => {
    const { fn } = fakeServer();
    render(
      <Providers>
        <MemoryPanel
          fetchFn={fn as unknown as FetchLike}
          highlight={['works as a data engineer in pune.']}
        />
      </Providers>,
    );
    await waitFor(() => expect(rows()).toHaveLength(3));
    expect(within(rows()[0]).getByText('Just saved')).toBeTruthy();
    expect(within(rows()[1]).queryByText('Just saved')).toBeNull();
  });
});

/* -------------------------------------------------------- entry points */

describe('where the panel opens from', () => {
  it('SettingsDialog has a Memory section that loads the panel', async () => {
    const { fn } = fakeServer();
    render(
      <Providers>
        <SettingsDialog
          open
          account={null}
          onClose={() => undefined}
          fetchFn={fn as unknown as FetchLike}
        />
      </Providers>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Memory' }));
    await waitFor(() => expect(rows()).toHaveLength(3));
  });

  it('Escape in a row confirm closes only the confirm, never Settings around it', async () => {
    const { fn, calls } = fakeServer();
    const onClose = vi.fn();
    render(
      <Providers>
        <SettingsDialog
          open
          initialSection="memory"
          account={null}
          onClose={onClose}
          fetchFn={fn as unknown as FetchLike}
        />
      </Providers>,
    );
    await waitFor(() => expect(rows()).toHaveLength(3));
    const del = within(rows()[2]).getByRole('button', { name: /Delete/ });
    fireEvent.click(del);
    const confirm = await screen.findByRole('alertdialog');
    const cancel = within(confirm).getByRole('button', { name: 'Cancel' });
    expect(document.activeElement).toBe(cancel);
    fireEvent.keyDown(cancel, { key: 'Escape' });

    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'Settings' })).toBeTruthy();
    // Back on the control that opened the confirm.
    expect(document.activeElement).toBe(del);
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0);
  });

  it('after a delete, focus moves to the next row rather than falling to <body>', async () => {
    const { fn } = fakeServer();
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(3));
    fireEvent.click(within(rows()[0]).getByRole('button', { name: /Delete/ }));
    fireEvent.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }),
    );
    await waitFor(() => expect(rows()).toHaveLength(2));
    await waitFor(() =>
      expect(document.activeElement).toBe(
        within(rows()[0]).getByRole('button', { name: /Delete/ }),
      ),
    );
    expect(rows()[0].textContent).toMatch(/Prefers answers in metric units/);
  });

  it('SettingsDialog can open straight onto the Memory section', async () => {
    const { fn } = fakeServer();
    render(
      <Providers>
        <SettingsDialog
          open
          initialSection="memory"
          account={null}
          onClose={() => undefined}
          fetchFn={fn as unknown as FetchLike}
        />
      </Providers>,
    );
    await waitFor(() => expect(rows()).toHaveLength(3));
  });

  it('the "Memory updated" chip under an answer opens the panel', async () => {
    const { fn } = fakeServer();
    vi.stubGlobal('fetch', fn);
    const message: ChatMessage = {
      id: 'a1',
      role: 'assistant',
      content: 'Noted.',
      status: 'done',
      createdAt: 0,
      meta: { memory_updated: ['Works as a data engineer in Pune'] },
    };
    render(
      <Providers>
        <MessageRow message={message} isLast onRegenerate={vi.fn()} onRetry={vi.fn()} />
      </Providers>,
    );
    // Nothing is fetched until the person asks to look.
    expect(fn).not.toHaveBeenCalled();
    const chip = screen.getByRole('button', { name: /Memory updated/ });
    expect(chip.getAttribute('aria-haspopup')).toBe('dialog');
    await act(async () => {
      fireEvent.click(chip);
    });

    const dialog = await screen.findByRole('dialog', { name: 'Memory' });
    await waitFor(() => expect(within(dialog).getAllByTestId('memory-fact')).toHaveLength(3));
    expect(within(dialog).getByText('Just saved')).toBeTruthy();

    // Escape on the panel closes it and hands focus back to the chip.
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: 'Memory' })).toBeNull();
    expect(document.activeElement).toBe(chip);
  });
});

/* ------------------------------------------- QA round 1 reproductions */

/**
 * ChatApp's window-level shortcuts, run through the REAL map in
 * lib/searchPalette with an answer streaming, plus a document-level Escape
 * listener like ArtifactPanel's. What they record is what a key pressed in a
 * memory dialog did to the page behind it.
 */
function pageBehind() {
  const actions: string[] = [];
  const documentEscapes: string[] = [];
  function onWindowKey(e: KeyboardEvent) {
    const target = e.target as HTMLElement | null;
    const action = shortcutAction(e, {
      paletteOpen: false,
      quoteActionOpen: false,
      streaming: true,
      typing:
        target?.tagName === 'INPUT' ||
        target?.tagName === 'TEXTAREA' ||
        target?.isContentEditable === true,
    });
    if (action) actions.push(action);
  }
  function onDocumentKey(e: KeyboardEvent) {
    if (e.key === 'Escape') documentEscapes.push('artifact-panel-closed');
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

/** What a click on plain text did in Chromium before the fix: focus to <body>. */
function focusFallsToBody() {
  act(() => {
    (document.activeElement as HTMLElement | null)?.blur();
  });
  expect(document.activeElement).toBe(document.body);
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

describe('keys pressed while a memory dialog is open never reach the page behind it', () => {
  let behind: ReturnType<typeof pageBehind>;
  beforeEach(() => {
    behind = pageBehind();
  });
  afterEach(() => behind.remove());

  it('chip dialog: Escape after focus falls to <body> closes the dialog and does not stop the answer', async () => {
    const { fn } = fakeServer();
    vi.stubGlobal('fetch', fn);
    render(
      <Providers>
        <MessageRow message={chipMessage()} isLast onRegenerate={vi.fn()} onRetry={vi.fn()} />
      </Providers>,
    );
    const chip = screen.getByRole('button', { name: /Memory updated/ });
    await act(async () => {
      fireEvent.click(chip);
    });
    const dialog = await screen.findByRole('dialog', { name: 'Memory' });
    await waitFor(() => expect(within(dialog).getAllByTestId('memory-fact')).toHaveLength(3));

    focusFallsToBody();
    fireEvent.keyDown(document.body, { key: 'Escape' });

    expect(screen.queryByRole('dialog', { name: 'Memory' })).toBeNull();
    expect(behind.actions).toEqual([]);
    expect(behind.documentEscapes).toEqual([]);
    expect(document.activeElement).toBe(chip);
  });

  it('clear-all dialog: Escape after focus falls to <body> closes only the clear-all dialog', async () => {
    const { fn, calls } = fakeServer();
    const onClose = vi.fn();
    render(
      <Providers>
        <MemoryDialog open onClose={onClose} fetchFn={fn as unknown as FetchLike} />
      </Providers>,
    );
    await waitFor(() => expect(rows()).toHaveLength(3));
    fireEvent.click(screen.getByRole('button', { name: 'Delete all' }));
    await screen.findByRole('alertdialog', { name: /Delete everything/ });

    focusFallsToBody();
    fireEvent.keyDown(document.body, { key: 'Escape' });

    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'Memory' })).toBeTruthy();
    expect(behind.actions).toEqual([]);
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(0);
  });

  it('Settings > Memory: Escape after focus falls to <body> closes Settings and does not stop the answer', async () => {
    const { fn } = fakeServer();
    const onClose = vi.fn();
    render(
      <Providers>
        <SettingsDialog
          open
          initialSection="memory"
          account={null}
          onClose={onClose}
          fetchFn={fn as unknown as FetchLike}
        />
      </Providers>,
    );
    await waitFor(() => expect(rows()).toHaveLength(3));

    focusFallsToBody();
    fireEvent.keyDown(document.body, { key: 'Escape' });

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(behind.actions).toEqual([]);
    expect(behind.documentEscapes).toEqual([]);
  });

  it('"/" and Ctrl+K do nothing behind the dialog, from <body> or from a control inside it', async () => {
    const { fn } = fakeServer();
    render(
      <Providers>
        <MemoryDialog open onClose={() => undefined} fetchFn={fn as unknown as FetchLike} />
      </Providers>,
    );
    await waitFor(() => expect(rows()).toHaveLength(3));
    const close = screen.getByRole('button', { name: 'Close memory' });
    fireEvent.keyDown(close, { key: '/' });
    fireEvent.keyDown(close, { key: 'k', ctrlKey: true });
    focusFallsToBody();
    fireEvent.keyDown(document.body, { key: '/' });
    fireEvent.keyDown(document.body, { key: 'k', ctrlKey: true });
    expect(behind.actions).toEqual([]);
    expect(screen.getByRole('dialog', { name: 'Memory' })).toBeTruthy();
  });

  it('each dialog panel can hold focus, so a click on its text keeps focus inside', async () => {
    const { fn } = fakeServer();
    const onClose = vi.fn();
    render(
      <Providers>
        <MemoryDialog open onClose={onClose} fetchFn={fn as unknown as FetchLike} />
      </Providers>,
    );
    await waitFor(() => expect(rows()).toHaveLength(3));
    fireEvent.click(screen.getByRole('button', { name: 'Delete all' }));
    const typed = await screen.findByRole('alertdialog', { name: /Delete everything/ });
    act(() => typed.focus());
    expect(document.activeElement).toBe(typed);
    fireEvent.keyDown(typed, { key: 'Escape' });
    expect(screen.queryByRole('alertdialog')).toBeNull();

    const memory = screen.getByRole('dialog', { name: 'Memory' });
    act(() => memory.focus());
    expect(document.activeElement).toBe(memory);
    fireEvent.keyDown(memory, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(behind.actions).toEqual([]);
  });

  it('the Settings panel can hold focus too', () => {
    render(
      <Providers>
        <SettingsDialog open account={null} onClose={() => undefined} fetchFn={vi.fn() as unknown as FetchLike} />
      </Providers>,
    );
    const settings = screen.getByRole('dialog', { name: 'Settings' });
    act(() => settings.focus());
    expect(document.activeElement).toBe(settings);
  });

  it('Tab wraps inside Settings instead of leaving for the page behind it', () => {
    render(
      <Providers>
        <SettingsDialog open account={null} onClose={() => undefined} fetchFn={vi.fn() as unknown as FetchLike} />
      </Providers>,
    );
    const close = screen.getByRole('button', { name: 'Close settings' });
    const help = screen.getByRole('button', { name: 'Help' });
    act(() => help.focus());
    fireEvent.keyDown(help, { key: 'Tab' });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(help);
  });

  it('Shift+Tab from the focused panel wraps to its last control; Tab from <body> comes back in', async () => {
    const { fn } = fakeServer();
    render(
      <Providers>
        <MemoryDialog open onClose={() => undefined} fetchFn={fn as unknown as FetchLike} />
      </Providers>,
    );
    await waitFor(() => expect(rows()).toHaveLength(3));
    const memory = screen.getByRole('dialog', { name: 'Memory' });
    const close = screen.getByRole('button', { name: 'Close memory' });
    const last = screen.getByRole('button', { name: 'Delete all' });
    act(() => memory.focus());
    fireEvent.keyDown(memory, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last);

    focusFallsToBody();
    fireEvent.keyDown(document.body, { key: 'Tab' });
    expect(document.activeElement).toBe(close);
  });

  it('Escape in a Sessions confirm closes only that confirm, never Settings around it', async () => {
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
        <SettingsDialog
          open
          initialSection="sessions"
          account={null}
          onClose={onClose}
          fetchFn={fn as unknown as FetchLike}
        />
      </Providers>,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    const confirm = await screen.findByRole('alertdialog');
    const cancel = within(confirm).getByRole('button', { name: 'Cancel' });
    expect(document.activeElement).toBe(cancel);
    fireEvent.keyDown(cancel, { key: 'Escape' });

    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole('dialog', { name: 'Settings' })).toBeTruthy();
    expect(behind.actions).toEqual([]);
  });
});

describe('the date a row shows (QA round 1)', () => {
  it('a fact the extractor rewrote shows the date its current text and quote were written', async () => {
    // db.update_user_fact sets fact, updated_at, source and source_excerpt
    // from the NEW message and leaves created_at alone.
    const rewritten: MemoryFact = {
      id: 1,
      fact: 'Works at Globex as a staff engineer',
      source_conversation_id: null,
      source: 'stated',
      source_excerpt: 'I moved to Globex last week as a staff engineer',
      created_at: '2026-03-02T10:00:00Z',
      updated_at: '2026-09-18T10:00:00Z',
    };
    const fn = vi.fn(async () => json({ facts: [rewritten] }));
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(1));
    const time = rows()[0].querySelector('time')!;
    expect(time.textContent).toBe(formatDay('2026-09-18T10:00:00Z'));
    expect(time.getAttribute('dateTime')).toBe('2026-09-18T10:00:00Z');
    // The first-save date is still one hover away.
    expect(time.getAttribute('title')).toMatch(/^Updated .* · first saved /);
  });

  it('a fact never rewritten says when it was saved', async () => {
    renderPanel(fakeServer().fn);
    await waitFor(() => expect(rows()).toHaveLength(3));
    const time = rows()[0].querySelector('time')!;
    expect(time.textContent).toBe(formatDay('2026-09-18T09:30:00Z'));
    expect(time.getAttribute('title')).toMatch(/^Saved /);
  });

  it('a malformed timestamp is not printed as a raw string', async () => {
    const odd: MemoryFact = { ...FACTS[0], created_at: 'yesterday-ish', updated_at: null };
    const fn = vi.fn(async () => json({ facts: [odd] }));
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(1));
    expect(rows()[0].textContent).not.toMatch(/yesterday-ish/);
    expect(rows()[0].querySelector('time')).toBeNull();
  });
});

describe('cutting long text (QA round 1)', () => {
  const loneSurrogate = /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

  it('a quote cut at the limit never splits an emoji into a lone surrogate', async () => {
    const excerpt = 'a'.repeat(139) + '😀' + ' tail text that pushes it over the limit';
    const fn = vi.fn(async () => json({ facts: [{ ...FACTS[0], source_excerpt: excerpt }] }));
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(1));
    expect(loneSurrogate.test(rows()[0].textContent ?? '')).toBe(false);
    expect(rows()[0].textContent).toContain('😀…');
  });

  it('a delete button label cut at 80 never splits an emoji', async () => {
    const text = 'b'.repeat(79) + '🙂' + ' and more words after it';
    const fn = vi.fn(async () => json({ facts: [{ ...FACTS[0], fact: text }] }));
    renderPanel(fn);
    await waitFor(() => expect(rows()).toHaveLength(1));
    const label = within(rows()[0]).getByRole('button').getAttribute('aria-label') ?? '';
    expect(loneSurrogate.test(label)).toBe(false);
    expect(label).toBe(`Delete “${'b'.repeat(79)}🙂…”`);
  });

  it('clipText keeps a joined emoji whole and leaves short text alone', () => {
    const family = '👨‍👩‍👧';
    expect(clipText('a'.repeat(9) + family + ' tail', 10)).toBe(`${'a'.repeat(9)}${family}…`);
    expect(clipText('  short\n text ', 10)).toBe('short text');
    expect(clipText(family.repeat(3), 3)).toBe(family.repeat(3));
  });
});

describe('failures are never shown as success (QA round 1)', () => {
  for (const status of [405, 500]) {
    it(`a ${status} on the clear-all keeps every row, says nothing was deleted, and claims no deletion`, async () => {
      // 405 is what 4810da0's orchestrator answers: DELETE /memory/facts does
      // not exist there until memory-recall-and-facts merges.
      const { fn, calls } = fakeServer((url, init) =>
        url === '/api/memory/facts?confirm=all' && init.method === 'DELETE'
          ? json({ detail: 'Method Not Allowed' }, status)
          : (undefined as unknown as Response),
      );
      renderPanel(fn);
      await waitFor(() => expect(rows()).toHaveLength(3));
      fireEvent.click(screen.getByRole('button', { name: 'Delete all' }));
      const dialog = await screen.findByRole('alertdialog', { name: /Delete everything/ });
      fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: CLEAR_ALL_PHRASE } });
      fireEvent.click(within(dialog).getByRole('button', { name: 'Delete all memory' }));

      expect((await screen.findByRole('alert')).textContent).toMatch(/was not deleted/);
      expect(rows()).toHaveLength(3);
      expect(screen.queryByText(/Nothing saved yet/)).toBeNull();
      expect(screen.queryByText(/Deleted \d+ saved fact/)).toBeNull();
      expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(1);
    });
  }

  it('a 200 without a facts list is the error state, not "Nothing saved yet"', async () => {
    const fn = vi.fn(async () => json({ items: [{ id: 1, fact: 'x' }] }));
    renderPanel(fn);
    expect((await screen.findByRole('alert')).textContent).toMatch(/Could not load your memory/);
    expect(screen.queryByText(/Nothing saved yet/)).toBeNull();
  });

  it('a 401 says the session ended rather than blaming the connection', async () => {
    const fn = vi.fn(async () => json({ detail: 'Not authenticated' }, 401));
    renderPanel(fn);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/session has ended/);
    expect(alert.textContent).not.toMatch(/connection/);
  });
});
