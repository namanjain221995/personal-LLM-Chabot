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

import { MemoryPanel } from '@/components/MemoryPanel';
import { MessageRow } from '@/components/MessageRow';
import { Providers } from '@/components/Providers';
import { SettingsDialog } from '@/components/SettingsDialog';
import type { FetchLike } from '@/lib/auth';
import {
  CLEAR_ALL_PHRASE,
  excerptQuote,
  sourceLabel,
  type MemoryFact,
} from '@/lib/memory';
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
