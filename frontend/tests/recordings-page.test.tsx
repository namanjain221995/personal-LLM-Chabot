// @vitest-environment jsdom
/**
 * "Your recordings" (voice security review, item 6): a person can find, play,
 * copy the transcript of, and delete every recording the microphone stored.
 *
 * Driven against a fake of the orchestrator's session routes as the proxy
 * serves them (/api/audio/sessions...), with the response shapes read from
 * orchestrator/app/dictation.py list_sessions() and state() on the backend
 * branch: {sessions: [...], next_before} and the flat {detail, reason} refusals.
 */

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AccountMenu, clearAccountCache } from '@/components/AccountMenu';
import { RecordingsPage } from '@/components/recordings/RecordingsPage';
import { formatWhen } from '@/lib/format';

afterEach(cleanup);
beforeEach(() => {
  clearAccountCache();
  HTMLMediaElement.prototype.play = HTMLMediaElement.prototype.play ?? (async () => undefined);
});

/* ------------------------------------------------------------ the fake */

interface Row {
  session_id: string;
  created_at: string;
  status: 'recording' | 'finishing' | 'done' | 'failed';
  outcome: string | null;
  audio_ms: number;
  bytes: number;
  mime_type: string;
  delete_after: string | null;
  preview: string | null;
  text?: string;
}

const id = (n: number) => n.toString(16).padStart(32, '0');

/** Python isoformat, as the server writes it: microseconds and "+00:00". */
function isoHoursAgo(hours: number): string {
  const d = new Date(Date.UTC(2026, 8, 29, 10, 0, 0) - hours * 3_600_000);
  return d.toISOString().replace('Z', '').replace(/\.(\d{3})$/, '.$1456') + '+00:00';
}

function row(n: number, over: Partial<Row> = {}): Row {
  return {
    session_id: id(n),
    created_at: isoHoursAgo(n),
    status: 'done',
    outcome: 'transcribed',
    audio_ms: 754_000,
    bytes: 12_150_000,
    mime_type: 'audio/webm',
    delete_after: null,
    preview: `Recording number ${n} begins with these words`,
    text: `Recording number ${n} begins with these words and carries on for twelve minutes.`,
    ...over,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

type DeleteBehaviour = 'ok' | 'gone' | 'server_error' | 'network' | 'voice_off' | 'hold';

class FakeSessions {
  rows: Row[];
  calls: { method: string; url: string }[] = [];
  deleteBehaviour: DeleteBehaviour = 'ok';
  listRefusal: { status: number; body: unknown } | null = null;
  pageSize = 20;
  /** 'hold': each DELETE waits here until the test releases it. */
  held = new Map<string, () => void>();
  /** What GET .../audio answers (the player's probe after an error). */
  audioBehaviour: 'ok' | 'archive_unavailable' | 'audio_missing' | 'network' = 'ok';
  audioRanges: (string | null)[] = [];

  constructor(rows: Row[]) {
    this.rows = rows;
  }

  fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const method = (init?.method ?? 'GET').toUpperCase();
    const url = new URL(String(input), 'http://app.test');
    this.calls.push({ method, url: String(input) });
    if (url.pathname === '/api/audio/sessions' && method === 'GET') {
      if (this.listRefusal) return json(this.listRefusal.body, this.listRefusal.status);
      const limit = Number(url.searchParams.get('limit'));
      const before = url.searchParams.get('before');
      if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
        return json({ detail: 'limit must be 1 to 100.', reason: 'bad_request' }, 400);
      }
      // The server's own rule: fromisoformat(before) — a "+" decoded as a
      // space is not ISO 8601 and is refused.
      if (before !== null && !/^\d{4}-\d\d-\d\dT[\d:.]+\+00:00$/.test(before)) {
        return json({ detail: 'before must be an ISO 8601 time.', reason: 'bad_request' }, 400);
      }
      const older = this.rows
        .filter((r) => before === null || r.created_at < before)
        .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
      const page = older.slice(0, limit);
      const more = older.length > limit;
      return json({
        // The list carries a preview, never the full text.
        sessions: page.map((r) => {
          const listed: Partial<Row> = { ...r };
          delete listed.text;
          return listed;
        }),
        next_before: more ? page[page.length - 1]!.created_at : null,
      });
    }
    const audio = url.pathname.match(/^\/api\/audio\/sessions\/([0-9a-f]{32})\/audio$/);
    if (audio && method === 'GET') {
      // Read as sent: jsdom's Headers drops `range` (a browser sends it).
      const sent = (init?.headers ?? {}) as Record<string, string>;
      this.audioRanges.push(
        Object.entries(sent).find(([name]) => name.toLowerCase() === 'range')?.[1] ?? null,
      );
      switch (this.audioBehaviour) {
        case 'network':
          throw new TypeError('Failed to fetch');
        case 'archive_unavailable':
          return json(
            {
              detail:
                "This recording is kept on the archive server, which isn't answering right now. Nothing is lost; try again in a few minutes.",
              reason: 'archive_unavailable',
              retry_after_s: 30,
            },
            503,
          );
        case 'audio_missing':
          return json({ detail: 'missing', reason: 'audio_missing' }, 410);
        default:
          return new Response(new Uint8Array([82]), {
            status: 206,
            headers: { 'content-type': 'audio/webm', 'content-range': 'bytes 0-0/12150000' },
          });
      }
    }
    const match = url.pathname.match(/^\/api\/audio\/sessions\/([0-9a-f]{32})$/);
    if (match && method === 'GET') {
      const r = this.rows.find((x) => x.session_id === match[1]);
      if (!r) return json({ detail: 'This recording is no longer on the server.', reason: 'not_found' }, 404);
      return json({
        session_id: r.session_id,
        status: r.status,
        rev: 7,
        audio_ms: r.audio_ms,
        cursor: 1,
        segments: [],
        gaps: [],
        outcome: r.outcome,
        text: r.status === 'done' ? (r.text ?? '') : null,
        stored: { kept: true, retention_days: 0, delete_after: null, bytes: r.bytes },
        error: null,
      });
    }
    if (match && method === 'DELETE') {
      switch (this.deleteBehaviour) {
        case 'hold':
          await new Promise<void>((resolve) => this.held.set(match[1]!, resolve));
          this.rows = this.rows.filter((x) => x.session_id !== match[1]);
          return new Response(null, { status: 204 });
        case 'network':
          throw new TypeError('Failed to fetch');
        case 'server_error':
          return json({ detail: 'The server could not remove the files.', reason: 'storage_unavailable' }, 503);
        case 'voice_off':
          return json({ detail: 'Voice input is turned off for your account.', reason: 'voice_off' }, 403);
        case 'gone':
          this.rows = this.rows.filter((x) => x.session_id !== match[1]);
          return json({ detail: 'This recording is no longer on the server.', reason: 'not_found' }, 404);
        default:
          this.rows = this.rows.filter((x) => x.session_id !== match[1]);
          return new Response(null, { status: 204 });
      }
    }
    return json({ detail: `unexpected ${method} ${String(input)}` }, 500);
  });

  asFetch(): typeof fetch {
    return this.fetch as unknown as typeof fetch;
  }

  count(method: string, pathPart = ''): number {
    return this.calls.filter((c) => c.method === method && c.url.includes(pathPart)).length;
  }
}

async function renderPage(fake: FakeSessions) {
  render(<RecordingsPage fetchFn={fake.asFetch()} />);
  await waitFor(() => expect(screen.queryByText('Loading your recordings…')).toBeNull());
}

function items(): HTMLElement[] {
  const list = screen.queryByRole('list', { name: 'Your recordings' });
  return list ? within(list).getAllByRole('listitem') : [];
}

function itemFor(n: number): HTMLElement {
  const title = document.getElementById(`recording-${id(n)}`);
  expect(title, `row ${n} is on the page`).not.toBeNull();
  return title!.closest('li')!;
}

/* ------------------------------------------------------------- tests */

describe('the list', () => {
  it('shows when, how long, the status and the transcript preview of each recording', async () => {
    const fake = new FakeSessions([
      row(1),
      row(2, { status: 'recording', outcome: null, audio_ms: 3_725_000, preview: null }),
      row(3, { status: 'finishing', outcome: null, preview: null }),
      row(4, { status: 'failed', outcome: 'undecodable', preview: null }),
    ]);
    await renderPage(fake);

    expect(items()).toHaveLength(4);
    const first = itemFor(1);
    expect(within(first).getByText(formatWhen(row(1).created_at))).toBeTruthy();
    expect(within(first).getByText('12:34')).toBeTruthy();
    expect(within(first).getByText('Done')).toBeTruthy();
    expect(within(first).getByText('Recording number 1 begins with these words')).toBeTruthy();

    const live = itemFor(2);
    expect(within(live).getByText('Recording')).toBeTruthy();
    // An hour and more, as the recorder shows it: h:mm:ss.
    expect(within(live).getByText('1:02:05')).toBeTruthy();
    expect(within(live).getByText('Still recording. The transcript appears once it ends.')).toBeTruthy();

    expect(within(itemFor(3)).getByText('Finishing')).toBeTruthy();
    expect(within(itemFor(4)).getByText('Failed')).toBeTruthy();
    expect(
      within(itemFor(4)).getByText(/couldn't read the audio in this recording/),
    ).toBeTruthy();
    expect(within(first).getByText('Kept until you delete it.')).toBeTruthy();
  });

  it('pages back in time with the cursor the server gave, encoded so its "+00:00" survives', async () => {
    const fake = new FakeSessions(Array.from({ length: 45 }, (_, i) => row(i + 1)));
    await renderPage(fake);
    expect(items()).toHaveLength(20);
    expect(fake.calls[0]!.url).toBe('/api/audio/sessions?limit=20');

    fireEvent.click(screen.getByRole('button', { name: 'Show older recordings' }));
    await waitFor(() => expect(items()).toHaveLength(40));
    const second = fake.calls.filter((c) => c.method === 'GET')[1]!.url;
    // The 20th row's created_at, with "+" as %2B: a raw "+" reaches the
    // server as a space and the page would stop at 20 with a 400.
    expect(second).toBe(
      `/api/audio/sessions?limit=20&before=${encodeURIComponent(row(20).created_at)}`,
    );
    expect(second).toContain('%2B00%3A00');

    fireEvent.click(screen.getByRole('button', { name: 'Show older recordings' }));
    await waitFor(() => expect(items()).toHaveLength(45));
    expect(screen.queryByRole('button', { name: 'Show older recordings' })).toBeNull();
    // Newest first all the way down, no row twice.
    const ids = items().map((li) => li.querySelector('h2')!.id);
    expect(new Set(ids).size).toBe(45);
    expect(ids[0]).toBe(`recording-${id(1)}`);
    expect(ids[44]).toBe(`recording-${id(45)}`);
  });

  it('explains, when there are none, that microphone recordings are kept here and can be deleted', async () => {
    const fake = new FakeSessions([]);
    await renderPage(fake);
    expect(screen.getByRole('heading', { name: 'No recordings yet' })).toBeTruthy();
    expect(
      screen.getByText(
        /When you use the microphone button in a chat, the recording is saved to your account and kept here with its transcript\. You can delete any of them at any time\./,
      ),
    ).toBeTruthy();
    expect(screen.queryByRole('list', { name: 'Your recordings' })).toBeNull();
  });

  it('says why the list could not be loaded, and offers sign-in when signed out', async () => {
    const off = new FakeSessions([row(1)]);
    off.listRefusal = {
      status: 403,
      body: { detail: 'Voice input is turned off for your account. Ask an administrator.', reason: 'voice_off' },
    };
    await renderPage(off);
    expect(screen.getByRole('alert').textContent).toContain(
      "Your recordings couldn't be loaded. Voice input is turned off for your account. Ask an administrator.",
    );
    cleanup();

    const out = new FakeSessions([row(1)]);
    out.listRefusal = { status: 401, body: { detail: 'Not signed in' } };
    await renderPage(out);
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain("Your recordings couldn't be loaded. You were signed out.");
    expect(within(alert).getByRole('link', { name: 'Sign in' }).getAttribute('href')).toBe('/login');
  });
});

describe('playing', () => {
  it('gives every recording a player that downloads nothing until Play', async () => {
    const fake = new FakeSessions([row(1), row(2)]);
    await renderPage(fake);
    const players = document.querySelectorAll('audio');
    expect(players).toHaveLength(2);
    for (const [i, player] of Array.from(players).entries()) {
      expect(player.getAttribute('preload')).toBe('none');
      expect(player.hasAttribute('autoplay')).toBe(false);
      expect(player.getAttribute('src')).toBe(`/api/audio/sessions/${id(i + 1)}/audio`);
      expect(player.hasAttribute('controls')).toBe(true);
      expect(player.getAttribute('aria-label')).toMatch(/^Play the recording from /);
    }
    // Rendering the list fetched the list and nothing else: no audio bytes.
    expect(fake.calls.map((c) => c.url)).toEqual(['/api/audio/sessions?limit=20']);
  });

  it('says why a player failed: the archive server is down, the audio is missing, or the format', async () => {
    const fake = new FakeSessions([row(1)]);
    await renderPage(fake);
    const player = itemFor(1).querySelector('audio')!;

    fake.audioBehaviour = 'archive_unavailable';
    fireEvent.error(player);
    await waitFor(() =>
      expect(within(itemFor(1)).getByRole('alert').textContent).toBe(
        "This recording is kept on the archive server, which isn't answering right now. Nothing is lost; try again in a few minutes.",
      ),
    );
    expect(fake.audioRanges).toEqual(['bytes=0-0']);

    fake.audioBehaviour = 'audio_missing';
    fireEvent.error(player);
    await waitFor(() =>
      expect(within(itemFor(1)).getByRole('alert').textContent).toContain(
        'could not be found on the archive server',
      ),
    );

    fake.audioBehaviour = 'ok';
    fireEvent.error(player);
    await waitFor(() =>
      expect(within(itemFor(1)).getByRole('alert').textContent).toContain(
        "This browser can't play this recording's format",
      ),
    );

    fake.audioBehaviour = 'network';
    fireEvent.error(player);
    await waitFor(() =>
      expect(within(itemFor(1)).getByRole('alert').textContent).toContain(
        "This recording couldn't be played here.",
      ),
    );
    // Pressing Play again clears the message.
    fireEvent.play(player);
    await waitFor(() => expect(within(itemFor(1)).queryByRole('alert')).toBeNull());
  });

  it('shows no player for a recording retention has already removed, but still lets it be deleted', async () => {
    const fake = new FakeSessions([
      row(1, { delete_after: '2026-01-01T00:00:00+00:00', preview: null }),
    ]);
    await renderPage(fake);
    const item = itemFor(1);
    expect(item.querySelector('audio')).toBeNull();
    expect(within(item).getByText('The audio and transcript are no longer on the server.')).toBeTruthy();
    expect(within(item).getByRole('button', { name: /^Delete/ })).toBeTruthy();
  });
});

describe('the transcript', () => {
  it('opens the full transcript with a plain read, and copies it', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const fake = new FakeSessions([row(1)]);
    await renderPage(fake);

    const toggle = within(itemFor(1)).getByRole('button', { name: 'Show transcript' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const full = row(1).text!;
    await waitFor(() => expect(screen.getByText(full)).toBeTruthy());
    // A plain read: no wait_s, so the recorder's long-poll never holds it open.
    expect(fake.calls[1]!.url).toBe(`/api/audio/sessions/${id(1)}`);

    fireEvent.click(within(itemFor(1)).getByRole('button', { name: 'Copy transcript' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(full));
  });

  it('says a recording is still being transcribed instead of showing half a transcript', async () => {
    const fake = new FakeSessions([row(1, { status: 'finishing', outcome: null, preview: null })]);
    await renderPage(fake);
    fireEvent.click(within(itemFor(1)).getByRole('button', { name: 'Show transcript' }));
    await waitFor(() =>
      expect(within(itemFor(1)).getAllByText('Being transcribed now.').length).toBeGreaterThan(1),
    );
    expect(within(itemFor(1)).queryByRole('button', { name: 'Copy transcript' })).toBeNull();
  });
});

describe('deleting', () => {
  it('asks first, and does nothing when the person cancels', async () => {
    const fake = new FakeSessions([row(1), row(2)]);
    await renderPage(fake);
    const del = within(itemFor(1)).getByRole('button', { name: /^Delete the recording from/ });
    fireEvent.click(del);
    const dialog = screen.getByRole('alertdialog', { name: 'Delete this recording?' });
    expect(dialog.textContent).toContain(
      `The 12:34 recording from ${formatWhen(row(1).created_at)} and its transcript will be deleted from the server.`,
    );
    expect(dialog.textContent).toContain("This can't be undone.");
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(fake.count('DELETE')).toBe(0);
    expect(items()).toHaveLength(2);
    expect(document.activeElement).toBe(del);
  });

  it('deletes after the confirmation, removes the row and says so', async () => {
    const fake = new FakeSessions([row(1), row(2)]);
    await renderPage(fake);
    fireEvent.click(within(itemFor(1)).getByRole('button', { name: /^Delete the recording from/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete recording' }));

    await waitFor(() => expect(items()).toHaveLength(1));
    expect(fake.calls.filter((c) => c.method === 'DELETE').map((c) => c.url)).toEqual([
      `/api/audio/sessions/${id(1)}`,
    ]);
    expect(document.getElementById(`recording-${id(1)}`)).toBeNull();
    expect(screen.getByRole('status').textContent).toBe(
      `Deleted the recording from ${formatWhen(row(1).created_at)}.`,
    );
    // Focus lands on the row that took its place, not on <body>.
    await waitFor(() => expect(document.activeElement?.id).toBe(`recording-${id(2)}`));
  });

  it('treats a recording that is already gone (404) as deleted', async () => {
    const fake = new FakeSessions([row(1)]);
    fake.deleteBehaviour = 'gone';
    await renderPage(fake);
    fireEvent.click(within(itemFor(1)).getByRole('button', { name: /^Delete the recording from/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete recording' }));
    await waitFor(() => expect(screen.getByRole('heading', { name: 'No recordings yet' })).toBeTruthy());
    await waitFor(() => expect(document.activeElement?.id).toBe('recordings-empty'));
  });

  it.each([
    ['server_error', 'Not deleted. The server answered with error 503: "The server could not remove the files." Try again.'],
    ['network', "Not deleted. The server couldn't be reached. Check your connection and try again."],
    ['voice_off', 'Not deleted. Voice input is turned off for your account. Ask an administrator.'],
  ] as const)('reports a %s failure as NOT deleted and keeps the row', async (behaviour, message) => {
    const fake = new FakeSessions([row(1), row(2)]);
    fake.deleteBehaviour = behaviour;
    await renderPage(fake);
    const del = within(itemFor(1)).getByRole('button', { name: /^Delete the recording from/ });
    fireEvent.click(del);
    fireEvent.click(screen.getByRole('button', { name: 'Delete recording' }));

    const alert = await within(itemFor(1)).findByRole('alert');
    expect(alert.textContent).toBe(message);
    expect(items()).toHaveLength(2);
    expect(screen.getByRole('status').textContent).toBe('');
    // One attempt, reported at once; the person decides to try again.
    expect(fake.count('DELETE')).toBe(1);
    await waitFor(() => expect((del as HTMLButtonElement).disabled).toBe(false));
    expect(document.activeElement).toBe(del);

    // Trying again once the server is back deletes it.
    fake.deleteBehaviour = 'ok';
    fireEvent.click(del);
    fireEvent.click(screen.getByRole('button', { name: 'Delete recording' }));
    await waitFor(() => expect(items()).toHaveLength(1));
  });

  it('keeps both rows gone when two deletes are in flight at once', async () => {
    const fake = new FakeSessions([row(1), row(2), row(3)]);
    fake.deleteBehaviour = 'hold';
    await renderPage(fake);
    for (const n of [1, 2]) {
      fireEvent.click(within(itemFor(n)).getByRole('button', { name: /^Delete the recording from/ }));
      fireEvent.click(screen.getByRole('button', { name: 'Delete recording' }));
    }
    await waitFor(() => expect(fake.held.size).toBe(2));

    fake.held.get(id(1))!();
    await waitFor(() => expect(items()).toHaveLength(2));
    fake.held.get(id(2))!();
    await waitFor(() => expect(document.getElementById(`recording-${id(2)}`)).toBeNull());
    // The second delete to finish must not bring the first one back.
    expect(items().map((li) => li.querySelector('h2')!.id)).toEqual([`recording-${id(3)}`]);
    await waitFor(() => expect(document.activeElement?.id).toBe(`recording-${id(3)}`));
  });

  it('warns that deleting a recording in progress stops it', async () => {
    const fake = new FakeSessions([row(1, { status: 'recording', outcome: null, preview: null })]);
    await renderPage(fake);
    fireEvent.click(within(itemFor(1)).getByRole('button', { name: /^Delete the recording from/ }));
    expect(screen.getByRole('alertdialog').textContent).toContain(
      'It is still in progress: deleting it stops it and removes what was saved so far.',
    );
  });
});

describe('finding the page', () => {
  it('is linked from the account menu as "Recordings"', async () => {
    const me = {
      username: 'naman',
      user: { id: 1, name: 'Naman Jain', email: 'naman@techsara.test' },
      workspace: { id: 'ws1', name: 'TechSara Solutions', role: 'member' },
      capabilities: [] as string[],
    };
    const fetchFn = vi.fn(async () => json(me));
    render(<AccountMenu fetchFn={fetchFn as unknown as typeof fetch} navigate={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /Naman Jain/ }));
    const link = screen.getByRole('menuitem', { name: 'Recordings' });
    expect(link.tagName).toBe('A');
    expect(link.getAttribute('href')).toBe('/recordings');
  });
});
