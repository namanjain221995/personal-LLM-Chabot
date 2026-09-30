// @vitest-environment jsdom
/**
 * The "My files" page (2026-09-30): a person finds everything they uploaded,
 * across every chat, and sees honestly what is still stored.
 *
 * Driven against a fake of the routes as the proxies serve them —
 * /api/files/mine and /summary (orchestrator/app/myfiles.py shapes), the
 * recording DELETE (/api/audio/sessions/{id}), and the preview reads the page
 * reuses (/api/uploads/{conv}/document, /api/uploads/{conv},
 * /api/uploads/{conv}/{upload}/file). The page URL is a reactive stand-in for
 * Next's router, so a filter change really does travel through the URL.
 */
import { act, cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ComponentProps, ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MyFilesPage } from '@/components/myfiles/MyFilesPage';
import { formatWhen } from '@/lib/format';

const nav = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  const state = { search: new URLSearchParams() };
  const set = (query: string) => {
    state.search = new URLSearchParams(query);
    listeners.forEach((listener) => listener());
  };
  return {
    state,
    listeners,
    set,
    replace: vi.fn((href: string) => set(href.includes('?') ? href.slice(href.indexOf('?') + 1) : '')),
  };
});

vi.mock('next/navigation', async () => {
  const React = await import('react');
  const subscribe = (listener: () => void) => {
    nav.listeners.add(listener);
    return () => {
      nav.listeners.delete(listener);
    };
  };
  const snapshot = () => nav.state.search;
  return {
    usePathname: () => '/files',
    useRouter: () => ({ replace: nav.replace, push: vi.fn() }),
    useSearchParams: () => React.useSyncExternalStore(subscribe, snapshot, snapshot),
  };
});

vi.mock('next/link', () => ({
  __esModule: true,
  default: (props: ComponentProps<'a'> & { children?: ReactNode }) => {
    const { children, ...rest } = props;
    return <a {...rest}>{children}</a>;
  },
}));

// A whole page render with effects and a debounce; the default 1 s budget is
// about a loaded CI runner, not about anything asserted here.
configure({ asyncUtilTimeout: 5000 });

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
beforeEach(() => {
  nav.set('');
  nav.replace.mockClear();
  HTMLMediaElement.prototype.play = HTMLMediaElement.prototype.play ?? (async () => undefined);
});

/* ------------------------------------------------------------ the fake */

const hex = (n: number) => n.toString(16).padStart(32, '0');

interface Row {
  id: string;
  source: 'upload' | 'text' | 'recording';
  kind: 'document' | 'dataset' | 'video' | 'audio' | 'recording';
  name: string;
  bytes: number | null;
  created_at: string;
  conversation: { id: string; title: string } | null;
  availability: 'available' | 'text_only' | 'summary_only' | 'processing' | 'expired';
  media: { status: string | null; duration_ms: number | null } | null;
  can: { download: boolean; preview: 'text' | 'summary' | 'audio' | null; delete: boolean };
}

function at(hoursAgo: number): string {
  return new Date(Date.UTC(2026, 8, 30, 10, 0, 0) - hoursAgo * 3_600_000).toISOString().replace('Z', '+00:00');
}

function upload(n: number, over: Partial<Row> = {}): Row {
  return {
    id: `upload:${hex(n)}`,
    source: 'upload',
    kind: 'document',
    name: `file-${n}.pdf`,
    bytes: 1000 + n,
    created_at: at(n),
    conversation: { id: 'conv-1', title: 'Quarterly planning' },
    availability: 'available',
    media: null,
    can: { download: true, preview: null, delete: false },
    ...over,
  };
}

function recording(n: number, over: Partial<Row> = {}): Row {
  return {
    id: `recording:${hex(1000 + n)}`,
    source: 'recording',
    kind: 'recording',
    name: 'Voice recording',
    bytes: 50_000 + n,
    created_at: at(n),
    conversation: null,
    availability: 'available',
    media: { status: 'done', duration_ms: 61_000 },
    can: { download: true, preview: 'audio', delete: true },
    ...over,
  };
}

const RETENTION = {
  upload_hours: 24,
  recording_days: 0,
  video_kept_with_chat: true,
  video_grace_hours: 72,
  pictures: 'browser_only',
  picture_memory_hours: 2,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function abortError(): Error {
  return Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' });
}

class FakeFiles {
  rows: Row[];
  retention: Record<string, unknown> = RETENTION;
  calls: { method: string; url: URL; signal: AbortSignal | undefined }[] = [];
  listRefusal: { status: number; body: unknown } | null = null;
  /** List requests whose q is here wait until the test releases them. */
  holdQuery: string | null = null;
  held: Array<() => void> = [];
  /** Extra rows served on the second page, to prove the page de-duplicates. */
  overlapOnSecondPage = false;
  fileStatus = 200;

  constructor(rows: Row[]) {
    this.rows = rows;
  }

  private matching(url: URL): Row[] {
    const q = (url.searchParams.get('q') ?? '').toLowerCase();
    const kinds = (url.searchParams.get('kind') ?? '').split(',').filter(Boolean);
    return this.rows
      .filter((r) => !kinds.length || kinds.includes(r.kind))
      .filter(
        (r) =>
          !q || r.name.toLowerCase().includes(q) || (r.conversation?.title ?? '').toLowerCase().includes(q),
      )
      .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
  }

  fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const method = (init?.method ?? 'GET').toUpperCase();
    const url = new URL(String(input), 'http://app.test');
    const signal = init?.signal ?? undefined;
    this.calls.push({ method, url, signal });
    if (signal?.aborted) throw abortError();

    if (url.pathname === '/api/files/mine' && method === 'GET') {
      if (this.holdQuery !== null && url.searchParams.get('q') === this.holdQuery) {
        await new Promise<void>((resolve, reject) => {
          this.held.push(resolve);
          signal?.addEventListener('abort', () => reject(abortError()));
        });
      }
      if (this.listRefusal) return json(this.listRefusal.body, this.listRefusal.status);
      const all = this.matching(url);
      const start = Number(url.searchParams.get('cursor') ?? '0');
      const limit = Number(url.searchParams.get('limit') ?? '50');
      let page = all.slice(start, start + limit);
      if (this.overlapOnSecondPage && start > 0) page = [all[start - 1]!, ...page];
      const next = start + limit < all.length ? String(start + limit) : null;
      return json({ items: page, next_cursor: next, retention: this.retention });
    }
    if (url.pathname === '/api/files/mine/summary' && method === 'GET') {
      const all = this.matching(new URL(`http://app.test/?q=${encodeURIComponent(url.searchParams.get('q') ?? '')}`));
      const kinds: Record<string, { count: number; bytes: number }> = {};
      for (const kind of ['document', 'dataset', 'video', 'audio', 'recording']) {
        const of = all.filter((r) => r.kind === kind);
        kinds[kind] = { count: of.length, bytes: of.reduce((n, r) => n + (r.bytes ?? 0), 0) };
      }
      return json({
        kinds,
        total: { count: all.length, bytes: all.reduce((n, r) => n + (r.bytes ?? 0), 0) },
        retention: this.retention,
      });
    }
    const recordingMatch = url.pathname.match(/^\/api\/audio\/sessions\/([0-9a-f]{32})$/);
    if (recordingMatch && method === 'DELETE') {
      this.rows = this.rows.filter((r) => r.id !== `recording:${recordingMatch[1]}`);
      return new Response(null, { status: 204 });
    }
    const documentMatch = url.pathname.match(/^\/api\/uploads\/([^/]+)\/document$/);
    if (documentMatch && method === 'GET') {
      return json({ filename: url.searchParams.get('name'), text: 'The text the chat read.', truncated: false });
    }
    const listMatch = url.pathname.match(/^\/api\/uploads\/([^/]+)$/);
    if (listMatch && method === 'GET') {
      return json({
        uploads: this.rows
          .filter((r) => r.source === 'upload')
          .map((r) => ({
            id: r.id.slice('upload:'.length),
            filename: r.name,
            status: 'expired',
            profile: [
              {
                file: r.name,
                kind: 'table',
                rows: 995,
                columns: [{ name: 'region' }, { name: 'amount' }],
                sample_rows: [{ region: 'north', amount: 10 }],
              },
            ],
          })),
      });
    }
    const fileMatch = url.pathname.match(/^\/api\/uploads\/([^/]+)\/([0-9a-f]{32})\/file$/);
    if (fileMatch && method === 'GET') {
      if (this.fileStatus === 410) return json({ message: 'this upload has expired' }, 410);
      return new Response(new Blob(['%PDF-1.4 bytes'], { type: 'application/pdf' }), { status: 200 });
    }
    return json({ detail: `unexpected ${method} ${url.pathname}` }, 500);
  });

  asFetch(): typeof fetch {
    return this.fetch as unknown as typeof fetch;
  }

  listCalls(): URL[] {
    return this.calls.filter((c) => c.method === 'GET' && c.url.pathname === '/api/files/mine').map((c) => c.url);
  }
}

async function renderPage(fake: FakeFiles) {
  // The preview loaders (lib/previewData, lib/attachments) use the global fetch.
  vi.stubGlobal('fetch', fake.fetch);
  render(<MyFilesPage fetchFn={fake.asFetch()} />);
  await waitFor(() => expect(screen.queryByText('Loading your files…')).toBeNull());
}

function list(): HTMLElement {
  return screen.getByRole('list', { name: 'Your files' });
}

function items(): HTMLElement[] {
  const found = screen.queryByRole('list', { name: 'Your files' });
  return found ? within(found).getAllByRole('listitem') : [];
}

function rowFor(name: string): HTMLElement {
  return within(list()).getByRole('heading', { name }).closest('li')!;
}

/* ------------------------------------------------------------- tests */

describe('the list', () => {
  it('shows a busy skeleton, then every file with its kind, size, time, chat and state', async () => {
    const fake = new FakeFiles([
      upload(1, { name: 'Q3 report.pdf', bytes: 2_400_000 }),
      upload(2, {
        id: 'text:42',
        source: 'text',
        name: 'old-contract.docx',
        bytes: null,
        availability: 'text_only',
        can: { download: false, preview: 'text', delete: false },
      }),
      upload(3, {
        kind: 'dataset',
        name: 'bundle.zip',
        availability: 'summary_only',
        can: { download: false, preview: 'summary', delete: false },
      }),
      recording(4),
    ]);
    vi.stubGlobal('fetch', fake.fetch);
    render(<MyFilesPage fetchFn={fake.asFetch()} />);
    expect(document.querySelector('[aria-busy="true"]')).not.toBeNull();
    await waitFor(() => expect(screen.queryByText('Loading your files…')).toBeNull());

    expect(items()).toHaveLength(4);
    const report = rowFor('Q3 report.pdf');
    expect(within(report).getByText('Document')).toBeTruthy();
    expect(within(report).getByText('2.3 MB')).toBeTruthy();
    expect(within(report).getByText(formatWhen(at(1)))).toBeTruthy();
    expect(within(report).getByText('Stored')).toBeTruthy();
    const chat = within(report).getByRole('link', { name: /Quarterly planning/ });
    expect(chat.getAttribute('href')).toBe('/?c=conv-1');

    expect(within(rowFor('old-contract.docx')).getByText('Text only')).toBeTruthy();
    expect(within(rowFor('bundle.zip')).getByText('Summary only')).toBeTruthy();
    const voice = rowFor('Voice recording');
    expect(within(voice).getByRole('link', { name: /Recordings/ }).getAttribute('href')).toBe('/recordings');
    expect(within(voice).getByText('1:01')).toBeTruthy();
  });

  it('announces how many files there are', async () => {
    const fake = new FakeFiles([upload(1), upload(2), recording(3)]);
    await renderPage(fake);
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/3 files/));
  });

  it('first run: says what will appear here, and that pictures are not stored', async () => {
    await renderPage(new FakeFiles([]));
    expect(screen.getByRole('heading', { name: 'No files yet' })).toBeTruthy();
    expect(screen.getAllByText(/Pictures stay only in the browser you sent them from/).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: 'Clear filters' })).toBeNull();
  });

  it('filtered to nothing: offers to clear the filters', async () => {
    nav.set('kind=video&q=zzz');
    const fake = new FakeFiles([upload(1)]);
    await renderPage(fake);
    expect(screen.getByRole('heading', { name: 'No files match these filters' })).toBeTruthy();
    const clear = screen.getAllByRole('button', { name: 'Clear filters' })[0]!;
    fireEvent.click(clear);
    await waitFor(() => expect(items()).toHaveLength(1));
    expect(nav.replace).toHaveBeenLastCalledWith('/files', { scroll: false });
  });

  it('a failed load says why and retries', async () => {
    const fake = new FakeFiles([upload(1)]);
    fake.listRefusal = { status: 500, body: { detail: 'database unavailable', reason: 'error' } };
    await renderPage(fake);
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toMatch(/Your files couldn't be loaded/);
    fake.listRefusal = null;
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(items()).toHaveLength(1));
  });

  it('signed out: offers Sign in instead of Retry', async () => {
    const fake = new FakeFiles([upload(1)]);
    fake.listRefusal = { status: 401, body: { detail: 'Not signed in.' } };
    await renderPage(fake);
    const alert = screen.getByRole('alert');
    expect(within(alert).getByRole('link', { name: 'Sign in' }).getAttribute('href')).toBe('/login');
    expect(within(alert).queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('loads older files without repeating one', async () => {
    const rows = Array.from({ length: 60 }, (_, n) => upload(n + 1));
    const fake = new FakeFiles(rows);
    fake.overlapOnSecondPage = true;
    await renderPage(fake);
    expect(items()).toHaveLength(50);
    fireEvent.click(screen.getByRole('button', { name: 'Show older files' }));
    await waitFor(() => expect(items()).toHaveLength(60));
    const ids = items().map((li) => li.getAttribute('aria-labelledby'));
    expect(new Set(ids).size).toBe(60);
    expect(screen.queryByRole('button', { name: 'Show older files' })).toBeNull();
  });
});

describe('filters', () => {
  it('shows each type with its count and filters through the URL', async () => {
    const fake = new FakeFiles([upload(1), upload(2), upload(3, { kind: 'dataset', name: 'sales.csv' }), recording(4)]);
    await renderPage(fake);
    const group = screen.getByRole('group', { name: 'Type' });
    await waitFor(() => expect(within(group).getByRole('radio', { name: /Documents.*2/ })).toBeTruthy());
    expect(within(group).getByRole('radio', { name: /All.*4/ })).toHaveProperty('checked', true);
    fireEvent.click(within(group).getByRole('radio', { name: /Voice recordings/ }));
    expect(nav.replace).toHaveBeenLastCalledWith('/files?kind=recording', { scroll: false });
    await waitFor(() => expect(items()).toHaveLength(1));
    expect(fake.listCalls().at(-1)!.searchParams.get('kind')).toBe('recording');
  });

  it('a second search cancels the first, so the results never arrive out of order', async () => {
    const fake = new FakeFiles([
      upload(1, { name: 'budget.xlsx', kind: 'dataset' }),
      upload(2, { name: 'budget q3.pdf' }),
      upload(3, { name: 'unrelated.pdf', conversation: { id: 'conv-2', title: 'Other' } }),
    ]);
    fake.holdQuery = 'budget';
    await renderPage(fake);
    const search = screen.getByRole('searchbox', { name: 'Search files and chats' });
    fireEvent.change(search, { target: { value: 'budget' } });
    await waitFor(() => expect(fake.held).toHaveLength(1));
    fireEvent.change(search, { target: { value: 'budget q3' } });
    await waitFor(() => expect(fake.listCalls().some((u) => u.searchParams.get('q') === 'budget q3')).toBe(true));
    const first = fake.calls.find((c) => c.url.searchParams.get('q') === 'budget');
    expect(first!.signal!.aborted).toBe(true);
    act(() => fake.held.forEach((release) => release()));
    await waitFor(() => expect(items().map((li) => within(li).getByRole('heading').textContent)).toEqual(['budget q3.pdf']));
    expect(nav.state.search.get('q')).toBe('budget q3');
  });

  it('collapses behind a Filters button that says whether it is open', async () => {
    await renderPage(new FakeFiles([upload(1)]));
    const toggle = screen.getByRole('button', { name: 'Filters' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    const panel = document.getElementById(toggle.getAttribute('aria-controls')!);
    expect(panel).not.toBeNull();
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
  });
});

describe('each row', () => {
  it('downloads through the streaming routes, with the file name in the link', async () => {
    const fake = new FakeFiles([upload(1, { name: 'Q3 report.pdf' }), recording(2)]);
    await renderPage(fake);
    const download = within(rowFor('Q3 report.pdf')).getByRole('link', { name: 'Download Q3 report.pdf' });
    expect(download.getAttribute('href')).toBe(`/api/uploads/conv-1/${hex(1)}/file`);
    expect(download.hasAttribute('download')).toBe(true);
    const voice = within(rowFor('Voice recording')).getByRole('link', { name: /^Download the voice recording from/ });
    expect(voice.getAttribute('href')).toBe(`/api/audio/sessions/${hex(1002)}/audio`);
    expect(voice.hasAttribute('download')).toBe(true);
    const player = rowFor('Voice recording').querySelector('audio')!;
    expect(player.getAttribute('preload')).toBe('none');
  });

  it('chat files have no delete; they say how to remove them', async () => {
    await renderPage(new FakeFiles([upload(1, { name: 'Q3 report.pdf' })]));
    const row = rowFor('Q3 report.pdf');
    expect(within(row).queryByRole('button', { name: /Delete/ })).toBeNull();
    expect(within(row).getByText('To remove it, delete its chat.')).toBeTruthy();
  });

  it('previews a text-only document from the text the chat read', async () => {
    const fake = new FakeFiles([
      upload(1, {
        id: 'text:42',
        source: 'text',
        name: 'old-contract.docx',
        bytes: null,
        availability: 'text_only',
        can: { download: false, preview: 'text', delete: false },
      }),
    ]);
    await renderPage(fake);
    fireEvent.click(within(rowFor('old-contract.docx')).getByRole('button', { name: 'Preview old-contract.docx' }));
    const dialog = await screen.findByRole('dialog', { name: 'Preview of old-contract.docx' });
    await waitFor(() => expect(within(dialog).getByText('The text the chat read.')).toBeTruthy());
    const read = fake.calls.find((c) => c.url.pathname === '/api/uploads/conv-1/document');
    expect(read!.url.searchParams.get('name')).toBe('old-contract.docx');
    expect(within(rowFor('old-contract.docx')).queryByRole('link', { name: /^Download/ })).toBeNull();
  });

  it('previews a spreadsheet summary after its bytes are gone', async () => {
    const fake = new FakeFiles([
      upload(1, {
        kind: 'dataset',
        name: 'sales.csv',
        availability: 'summary_only',
        can: { download: false, preview: 'summary', delete: false },
      }),
    ]);
    await renderPage(fake);
    fireEvent.click(within(rowFor('sales.csv')).getByRole('button', { name: 'Preview sales.csv' }));
    const dialog = await screen.findByRole('dialog', { name: 'Preview of sales.csv' });
    await waitFor(() => expect(within(dialog).getByText('north')).toBeTruthy());
    expect(within(dialog).getByText(/Showing 1 preview row of 995 rows/)).toBeTruthy();
  });

  it('a file that expired while the page was open turns into Removed when previewed', async () => {
    const fake = new FakeFiles([upload(1, { name: 'Q3 report.pdf' })]);
    fake.fileStatus = 410;
    await renderPage(fake);
    const row = rowFor('Q3 report.pdf');
    fireEvent.click(within(row).getByRole('button', { name: 'Preview Q3 report.pdf' }));
    const dialog = await screen.findByRole('dialog', { name: 'Preview of Q3 report.pdf' });
    await waitFor(() => expect(within(dialog).getByText(/has expired and is no longer stored/)).toBeTruthy());
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close preview' }));
    await waitFor(() => expect(within(rowFor('Q3 report.pdf')).getByText('Removed')).toBeTruthy());
    expect(within(rowFor('Q3 report.pdf')).queryByRole('link', { name: /^Download/ })).toBeNull();
  });

  it('the retention sentence is the server’s', async () => {
    const fake = new FakeFiles([upload(1)]);
    fake.retention = { ...RETENTION, upload_hours: 36 };
    await renderPage(fake);
    expect(screen.getByText(/Files you attach to a chat are kept for 36 hours/)).toBeTruthy();
    expect(screen.getByText(/Pictures stay only in the browser you sent them from/)).toBeTruthy();
  });
});

describe('deleting a recording', () => {
  it('confirms, deletes, announces it and moves focus to the next row', async () => {
    const fake = new FakeFiles([recording(1), recording(2), recording(3), upload(4, { name: 'Q3 report.pdf' })]);
    await renderPage(fake);
    const second = items()[1]!;
    const when = formatWhen(at(2));
    fireEvent.click(within(second).getByRole('button', { name: `Delete the voice recording from ${when}` }));
    const dialog = screen.getByRole('alertdialog', { name: 'Delete this recording?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete recording' }));
    await waitFor(() => expect(items()).toHaveLength(3));
    expect(fake.calls.some((c) => c.method === 'DELETE' && c.url.pathname === `/api/audio/sessions/${hex(1002)}`)).toBe(
      true,
    );
    const third = items()[1]!;
    await waitFor(() => expect(document.activeElement).toBe(within(third).getByRole('heading')));
    expect(screen.getByRole('status').textContent).toBe(`Deleted the voice recording from ${when}.`);
  });
});
