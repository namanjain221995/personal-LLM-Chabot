// @vitest-environment jsdom
/**
 * The side panel's keyboard and announcement contract.
 *
 * Escape ordering is the one that bites: ChatApp's window-level shortcut map
 * turns a bare Escape into "stop generating" while a stream is live. The
 * panel must consume Escape at the DOCUMENT level (before window) and stop
 * propagation, exactly as ActivityPanel does — so the test installs a window
 * listener and asserts it never hears the key. Unlike ActivityPanel the
 * desktop panel is persistent and non-modal, so an Escape that belongs to
 * the composer, to another dialog, or that a closer handler already
 * preventDefault()ed must NOT close it — and Tab is trapped only in the
 * under-768 px sheet, where the panel is genuinely modal.
 *
 * Since 2026-09-12 (CONTRACT-2 §9) the panel is opened on ONE FILE and steps
 * prev/next across the version's files and the message's versions; the
 * second half of this file pins that order, the disabled ends, the CSV grid
 * route, and the download fallback for a file with no preview.
 */
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ArtifactPanel, navigationEntries, SHEET_MAX_WIDTH } from '@/components/artifacts/ArtifactPanel';
import type { ArtifactFile, ArtifactRef } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/**
 * The viewport the panel believes it is in, as a WIDTH: the stub answers a
 * `(max-width: Npx)` query the way a browser N px wide would, so the test
 * and the component agree on the breakpoint through the query itself, not
 * through a string both happen to spell the same way. CONTRACT-2 §9 moved
 * the sheet from < 900 px to < 768 px (mobile).
 */
function viewportWidth(width: number) {
  vi.stubGlobal('matchMedia', (query: string) => {
    const m = /\(max-width:\s*(\d+)px\)/.exec(query);
    return {
      matches: m ? width <= Number(m[1]) : false,
      media: query,
      onchange: null,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      dispatchEvent: () => false,
    };
  });
}
function viewport(mode: 'sheet' | 'desktop') {
  viewportWidth(mode === 'sheet' ? 400 : 1440);
}

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const JOB = 'ffffffffffffffffffffffffffffffff';

const ref = (over: Partial<ArtifactRef> = {}): ArtifactRef => ({
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
  // 'none' keeps this test about the panel, not the viewers.
  preview_kind: 'none',
  preview_pages: 0,
  preview_url: '',
  thumbnail_url: '',
  warnings: [],
  created_at: '2026-09-11T10:00:00Z',
  operation: 'create',
  status_url: `/artifacts/jobs/${JOB}`,
  ...over,
});

function serveVersion(body: unknown, status = 200) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: status < 400, status, json: async () => body }) as unknown as Response),
  );
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

/** A card button in the document, the way ArtifactCard renders one. */
function mountOrigin(): HTMLButtonElement {
  const origin = document.createElement('button');
  origin.id = `artifact-card-${ID}-v1`;
  origin.textContent = 'Open Board deck';
  document.body.appendChild(origin);
  origin.focus();
  return origin;
}

describe('ArtifactPanel — dialog semantics and focus', () => {
  it('is a labelled dialog, takes focus on open and returns it to the originating card on close', async () => {
    serveVersion(ref());
    const origin = mountOrigin();
    expect(document.activeElement).toBe(origin);

    const onClose = vi.fn();
    const { unmount } = render(
      <ArtifactPanel artifactId={ID} version={1} originId={origin.id} onClose={onClose} />,
    );
    const dialog = screen.getByRole('dialog');
    const labelledBy = dialog.getAttribute('aria-labelledby');
    expect(labelledBy).toBeTruthy();
    // Focus moved INTO the panel.
    expect(dialog.contains(document.activeElement)).toBe(true);

    await flush();
    expect(document.getElementById(labelledBy!)?.textContent).toBe('Board deck');

    // Closing (unmount) hands focus back to the exact card that opened it.
    unmount();
    expect(document.activeElement).toBe(origin);
    origin.remove();
  });

  async function mountWithStops() {
    serveVersion(ref());
    const origin = mountOrigin();
    render(<ArtifactPanel artifactId={ID} version={1} originId={origin.id} onClose={vi.fn()} />);
    await flush();
    const dialog = screen.getByRole('dialog');
    const stops = Array.from(
      dialog.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),[tabindex]:not([tabindex="-1"])'),
    );
    expect(stops.length).toBeGreaterThan(1);
    return { origin, first: stops[0], last: stops[stops.length - 1] };
  }

  it('traps Tab inside the panel in both directions while it is the full-screen sheet', async () => {
    viewport('sheet');
    const { origin, first, last } = await mountWithStops();

    last.focus();
    fireEvent.keyDown(last, { key: 'Tab' });
    expect(document.activeElement).toBe(first);

    first.focus();
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last);
    origin.remove();
  });

  it('lets Tab leave the panel beside the conversation — the composer must stay reachable', async () => {
    viewport('desktop');
    const { origin, first, last } = await mountWithStops();

    // jsdom moves focus for nobody: if the trap stayed out of it, focus is
    // exactly where it was and the event went on its way (not prevented).
    last.focus();
    const forward = fireEvent.keyDown(last, { key: 'Tab' });
    expect(forward).toBe(true);
    expect(document.activeElement).toBe(last);

    first.focus();
    const backward = fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
    expect(backward).toBe(true);
    expect(document.activeElement).toBe(first);
    origin.remove();
  });

  it('is not marked modal on desktop: no aria-modal, so assistive tech keeps the page', async () => {
    viewport('desktop');
    serveVersion(ref());
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={vi.fn()} />);
    expect(screen.getByRole('dialog').getAttribute('aria-modal')).toBeNull();
  });
});

describe('ArtifactPanel — Escape', () => {
  it('closes on Escape and the window-level handler never hears it', async () => {
    serveVersion(ref());
    const onClose = vi.fn();
    const windowSaw = vi.fn();
    window.addEventListener('keydown', windowSaw);
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={onClose} />);
    await flush();

    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(windowSaw).not.toHaveBeenCalled();

    // Any other key still travels normally.
    fireEvent.keyDown(document.body, { key: 'a' });
    expect(windowSaw).toHaveBeenCalledTimes(1);
    window.removeEventListener('keydown', windowSaw);
  });

  it('leaves an Escape typed into a field outside the panel alone (the field owns it)', async () => {
    viewport('desktop');
    serveVersion(ref());
    const onClose = vi.fn();
    const composer = document.createElement('textarea');
    document.body.appendChild(composer);
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={onClose} />);
    await flush();

    composer.focus();
    const travelled = fireEvent.keyDown(composer, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
    // Not prevented either: ChatApp's map still sees it (and, with `typing`, ignores it).
    expect(travelled).toBe(true);

    // The same key from inside the panel still closes it.
    fireEvent.keyDown(screen.getByRole('button', { name: 'Close preview' }), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    composer.remove();
  });

  it('leaves an Escape another handler already answered alone', async () => {
    viewport('desktop');
    serveVersion(ref());
    const onClose = vi.fn();
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={onClose} />);
    await flush();

    // The palette, a menu, the mermaid viewer: React root handlers run
    // before the document listener and preventDefault first.
    const closer = (e: Event) => e.preventDefault();
    document.body.addEventListener('keydown', closer);
    fireEvent.keyDown(document.body, { key: 'Escape' });
    document.body.removeEventListener('keydown', closer);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('leaves an Escape aimed at another open dialog alone on desktop', async () => {
    viewport('desktop');
    serveVersion(ref());
    const onClose = vi.fn();
    const other = document.createElement('div');
    other.setAttribute('role', 'dialog');
    const button = document.createElement('button');
    other.appendChild(button);
    document.body.appendChild(other);
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={onClose} />);
    await flush();

    fireEvent.keyDown(button, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
    other.remove();
  });

  it('as the full-screen sheet, every Escape is its own — a field under it cannot have focus', async () => {
    viewport('sheet');
    serveVersion(ref());
    const onClose = vi.fn();
    const field = document.createElement('input');
    document.body.appendChild(field);
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={onClose} />);
    await flush();

    fireEvent.keyDown(field, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    field.remove();
  });

  it('the Back and Close controls both close', async () => {
    serveVersion(ref());
    const onClose = vi.fn();
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={onClose} />);
    await flush();
    fireEvent.click(screen.getByRole('button', { name: 'Close preview' }));
    fireEvent.click(screen.getByRole('button', { name: 'Back to the conversation' }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});

describe('ArtifactPanel — status is announced', () => {
  it('announces loading, then the ready line, in a polite live region', async () => {
    serveVersion(ref());
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={vi.fn()} />);
    const live = screen.getByTestId('artifact-panel-status');
    expect(live.getAttribute('role')).toBe('status');
    expect(live.getAttribute('aria-live')).toBe('polite');
    expect(live.textContent).toContain('Loading');
    await flush();
    expect(live.textContent).toBe('Board deck: Ready');
    // The header offers the distinct download for the one file.
    expect(screen.getByRole('link', { name: 'Download board-deck-v1.pptx' })).toBeTruthy();
    // CONTRACT-2 §9 / wave-4 brief: a deck whose preview the server did not
    // render says "Preview unavailable — download file" (was "There is
    // nothing to preview for this file."), and offers the download in the body too.
    expect(screen.getByText('Preview unavailable — download file')).toBeTruthy();
    expect(screen.getByTestId('artifact-panel-download-fallback').getAttribute('href')).toBe(
      `/api/artifacts/${ID}/v/1/file/pptx?disposition=attachment`,
    );
  });

  it('shows the truthful stage while the version is still being generated', async () => {
    const running = ref({ status: 'running', files: [] });
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith(`/v/1`)) {
        return { ok: true, status: 200, json: async () => running } as unknown as Response;
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({ job_id: JOB, artifact_id: ID, version: 1, status: 'running', stage: 'compose', stage_title: 'Writing the content' }),
      } as unknown as Response;
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={vi.fn()} />);
    await flush();
    await flush();
    expect(screen.getByText('Still being generated')).toBeTruthy();
    expect(document.body.textContent).toContain('Writing the content');
    expect(document.body.textContent).not.toMatch(/\d+%/);
    // No download is offered for a file that does not exist yet.
    expect(screen.queryByRole('link', { name: /Download/ })).toBeNull();
  });

  it('says permission denied for a 403 and unavailable for a 404, without a retry button', async () => {
    serveVersion({ detail: 'You do not have access to this file.' }, 403);
    const { unmount } = render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={vi.fn()} />);
    await flush();
    expect(screen.getAllByText('You do not have access to this file.').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
    unmount();

    serveVersion({ detail: 'Not found.' }, 404);
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={vi.fn()} />);
    await flush();
    expect(screen.getAllByText('This file is no longer available.').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  });

  it('offers a retry for a server failure', async () => {
    serveVersion({ detail: 'The server could not answer right now.' }, 503);
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={vi.fn()} />);
    await flush();
    expect(screen.getByText('The preview could not be loaded.')).toBeTruthy();
    serveVersion(ref());
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await flush();
    expect(screen.getByTestId('artifact-panel-status').textContent).toBe('Board deck: Ready');
  });
});

/* ---------------------------------------- one file at a time (CONTRACT-2 §9) */

const ID2 = 'b3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';

const contractFile = (
  artifactId: string,
  version: number,
  format: string,
  fileId: string,
  over: Partial<ArtifactFile> = {},
): ArtifactFile => ({
  file_id: fileId,
  role: format === 'csv' ? 'data' : format === 'pdf' ? 'companion' : 'primary',
  format,
  filename: `audit-v${version}.${format}`,
  title: 'IR Session Audit',
  mime_type: 'application/octet-stream',
  size: 1000,
  download_url: `/artifacts/${artifactId}/v/${version}/f/${fileId}?disposition=attachment`,
  inline_url: `/artifacts/${artifactId}/v/${version}/f/${fileId}?disposition=inline`,
  preview_url:
    format === 'xlsx' || format === 'csv'
      ? `/artifacts/${artifactId}/v/${version}/grid?file=${fileId}`
      : `/artifacts/${artifactId}/v/${version}/preview`,
  ...over,
});

/** A workbook version of four files, as the audit case produces it. */
const audit = (version = 1, over: Partial<ArtifactRef> = {}): ArtifactRef => ({
  artifact_id: ID,
  version,
  job_id: JOB,
  title: 'IR Session Audit',
  kind: 'workbook',
  status: 'completed',
  files: [
    contractFile(ID, version, 'xlsx', `a${version}00000000000001`, { sheets: 1, rows: 30, columns: 11 }),
    contractFile(ID, version, 'csv', `a${version}00000000000002`, { rows: 30, columns: 11 }),
    contractFile(ID, version, 'docx', `a${version}00000000000003`, { pages: 3 }),
    contractFile(ID, version, 'pdf', `a${version}00000000000004`, { pages: 3 }),
  ],
  preview_kind: 'grid',
  preview_pages: 0,
  preview_url: `/artifacts/${ID}/v/${version}/grid?file=a${version}00000000000001`,
  thumbnail_url: '',
  warnings: [],
  created_at: '2026-09-12T10:00:00Z',
  operation: version > 1 ? 'edit' : 'create',
  status_url: `/artifacts/jobs/${JOB}`,
  download_all_url: `/artifacts/${ID}/v/${version}/zip`,
  package: { count: 4 },
  ...over,
});

const grid = {
  sheets: ['Audit'],
  sheet: 'Audit',
  columns: ['Host', 'Candidate'],
  rows: [['Priya', 'CAND-0001']],
  total_rows: 30,
  total_columns: 11,
  truncated: true,
  formulas_as_text: true,
};

/** Serve every version in `versions` and the grid for any file. */
function serveVersions(versions: ArtifactRef[]) {
  const fetchMock = vi.fn(async (url: string) => {
    const m = /\/api\/artifacts\/([a-f0-9]{32})\/v\/(\d+)(\/grid|\/sheets)?/.exec(url);
    if (m && m[3]) {
      return { ok: true, status: 200, json: async () => grid } as unknown as Response;
    }
    const found = m ? versions.find((v) => v.artifact_id === m[1] && v.version === Number(m[2])) : undefined;
    if (found) return { ok: true, status: 200, json: async () => found } as unknown as Response;
    return { ok: false, status: 404, json: async () => ({ detail: 'Not found.' }) } as unknown as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('ArtifactPanel — one file, prev/next', () => {
  it('walks the version\'s files in order, then the message\'s other versions; disabled at the ends', () => {
    const v1 = audit(1);
    const v2 = audit(2);
    const other: ArtifactRef = {
      ...ref({ artifact_id: ID2, title: 'Memo', kind: 'document', preview_kind: 'pages', preview_pages: 2 }),
      files: [contractFile(ID2, 1, 'docx', 'c000000000000001', { pages: 2 }), contractFile(ID2, 1, 'pdf', 'c000000000000002', { pages: 2 })],
    };
    const running = audit(3, { status: 'running', files: [] });
    // Meta order: the memo first, then the audit's v2 before v1, then a
    // version still running — the entries follow the cards' order (by
    // artifact, versions ascending) and skip what has no files.
    const entries = navigationEntries([other, v2, v1, running], null);
    expect(entries.map((e) => `${e.artifactId.slice(0, 1)}:${e.version}:${e.file.format}`)).toEqual([
      'b:1:docx',
      'b:1:pdf',
      'a:1:xlsx',
      'a:1:csv',
      'a:1:docx',
      'a:1:pdf',
      'a:2:xlsx',
      'a:2:csv',
      'a:2:docx',
      'a:2:pdf',
    ]);
    // The version on show contributes the FETCHED copy of itself (fresher,
    // and the one with ids), in the same place.
    const fetched = audit(1, { files: [contractFile(ID, 1, 'csv', 'a100000000000002', { rows: 30, columns: 11 })] });
    expect(navigationEntries([v2, v1], fetched).map((e) => `${e.version}:${e.file.format}`)).toEqual([
      '1:csv',
      '2:xlsx',
      '2:csv',
      '2:docx',
      '2:pdf',
    ]);
    // A version the message did not list is still walkable on its own.
    expect(navigationEntries([], fetched).length).toBe(1);
  });

  it('opens on the file asked for, steps with Next and Previous, tells the host, and disables the ends', async () => {
    const v1 = audit(1);
    serveVersions([v1]);
    const onNavigate = vi.fn();
    render(
      <ArtifactPanel
        refs={[v1]}
        artifactId={ID}
        version={1}
        fileKey="a100000000000002"
        originId={null}
        onClose={vi.fn()}
        onNavigate={onNavigate}
      />,
    );
    await flush();
    await flush();
    // The header names the FILE: title · format · vN, and its own Download.
    expect(screen.getByRole('dialog').getAttribute('aria-labelledby')).toBe('artifact-panel-title');
    expect(document.getElementById('artifact-panel-title')?.textContent).toBe('IR Session Audit');
    expect(screen.getByTestId('artifact-panel-subtitle').textContent).toBe('CSV · v1 · 2 of 4');
    expect(screen.getByRole('link', { name: 'Download audit-v1.csv' }).getAttribute('href')).toBe(
      `/api/artifacts/${ID}/v/1/f/a100000000000002?disposition=attachment`,
    );
    const prev = screen.getByRole('button', { name: 'Previous file' });
    const next = screen.getByRole('button', { name: 'Next file' });
    expect(prev.hasAttribute('disabled')).toBe(false);
    expect(next.hasAttribute('disabled')).toBe(false);

    fireEvent.click(next);
    await flush();
    expect(onNavigate).toHaveBeenLastCalledWith(ID, 1, 'a100000000000003');
    expect(screen.getByTestId('artifact-panel-subtitle').textContent).toBe('Word · v1 · 3 of 4');
    fireEvent.click(screen.getByRole('button', { name: 'Next file' }));
    await flush();
    expect(screen.getByTestId('artifact-panel-subtitle').textContent).toBe('PDF · v1 · 4 of 4');
    // The last file: Next is disabled, Previous is not.
    expect(screen.getByRole('button', { name: 'Next file' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByRole('button', { name: 'Previous file' }).hasAttribute('disabled')).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Previous file' }));
    fireEvent.click(screen.getByRole('button', { name: 'Previous file' }));
    fireEvent.click(screen.getByRole('button', { name: 'Previous file' }));
    await flush();
    expect(screen.getByTestId('artifact-panel-subtitle').textContent).toBe('Excel · v1 · 1 of 4');
    expect(screen.getByRole('button', { name: 'Previous file' }).hasAttribute('disabled')).toBe(true);
    expect(onNavigate).toHaveBeenLastCalledWith(ID, 1, 'a100000000000001');
  });

  it('steps across versions: Next from the last file of v1 fetches v2 and shows its first file', async () => {
    const v1 = audit(1);
    const v2 = audit(2);
    const fetchMock = serveVersions([v1, v2]);
    const onNavigate = vi.fn();
    render(
      <ArtifactPanel
        refs={[v1, v2]}
        artifactId={ID}
        version={1}
        fileKey="a100000000000004"
        originId={null}
        onClose={vi.fn()}
        onNavigate={onNavigate}
      />,
    );
    await flush();
    await flush();
    expect(screen.getByTestId('artifact-panel-subtitle').textContent).toBe('PDF · v1 · 4 of 8');
    fireEvent.click(screen.getByRole('button', { name: 'Next file' }));
    await flush();
    await flush();
    expect(onNavigate).toHaveBeenLastCalledWith(ID, 2, 'a200000000000001');
    expect(fetchMock.mock.calls.some(([url]) => String(url) === `/api/artifacts/${ID}/v/2`)).toBe(true);
    expect(screen.getByTestId('artifact-panel-subtitle').textContent).toBe('Excel · v2 · 5 of 8');
    expect(screen.getByRole('link', { name: 'Download audit-v2.xlsx' })).toBeTruthy();
  });

  it('loads a CSV through grid?file=<its id> and an xlsx through its own', async () => {
    const v1 = audit(1);
    const fetchMock = serveVersions([v1]);
    render(
      <ArtifactPanel refs={[v1]} artifactId={ID} version={1} fileKey="a100000000000002" originId={null} onClose={vi.fn()} />,
    );
    await flush();
    await flush();
    expect(screen.getByTestId('sheet-viewer')).toBeTruthy();
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toContain(
      `/api/artifacts/${ID}/v/1/grid?file=a100000000000002&limit=200`,
    );
    expect(screen.getByTestId('sheet-size').textContent).toBe('30 rows · 11 columns');
    fireEvent.click(screen.getByRole('button', { name: 'Previous file' }));
    await flush();
    await flush();
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toContain(
      `/api/artifacts/${ID}/v/1/grid?file=a100000000000001&limit=200`,
    );
    expect(fetchMock.mock.calls.every(([url]) => !String(url).includes('/sheets'))).toBe(true);
  });

  it('finds the file by its legacy key when the fetched version has since gained ids', async () => {
    // The card was rendered from a history row without file ids; the server
    // (pipeline.ref_for) has upgraded the version since.
    const v1 = audit(1);
    serveVersions([v1]);
    render(
      <ArtifactPanel
        refs={[v1]}
        artifactId={ID}
        version={1}
        fileKey={`${ID}:1:docx:audit-v1.docx`}
        originId={null}
        onClose={vi.fn()}
      />,
    );
    await flush();
    await flush();
    expect(screen.getByTestId('artifact-panel-subtitle').textContent).toBe('Word · v1 · 3 of 4');
  });

  it('shows the download fallback for a file whose preview failed, and keeps prev/next working', async () => {
    const noPreview = audit(1, {
      preview_kind: 'none',
      files: [
        contractFile(ID, 1, 'docx', 'a100000000000003', { pages: 3, preview_url: '' }),
        contractFile(ID, 1, 'pdf', 'a100000000000004', { pages: 3, preview_url: '' }),
      ],
    });
    serveVersions([noPreview]);
    render(
      <ArtifactPanel refs={[noPreview]} artifactId={ID} version={1} fileKey="a100000000000003" originId={null} onClose={vi.fn()} />,
    );
    await flush();
    await flush();
    expect(screen.getByText('Preview unavailable — download file')).toBeTruthy();
    const fallback = screen.getByTestId('artifact-panel-download-fallback');
    expect(fallback.getAttribute('href')).toBe(`/api/artifacts/${ID}/v/1/f/a100000000000003?disposition=attachment`);
    expect(fallback.getAttribute('download')).toBe('audit-v1.docx');
    expect(screen.queryByTestId('pages-viewer')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Next file' }));
    await flush();
    expect(screen.getByTestId('artifact-panel-download-fallback').getAttribute('download')).toBe('audit-v1.pdf');
  });
});

describe('ArtifactPanel — focus and the mobile sheet', () => {
  it('Escape closes and focus returns to the card that opened it', async () => {
    viewport('desktop');
    const v1 = audit(1);
    serveVersions([v1]);
    const origin = document.createElement('button');
    origin.id = 'artifact-file-a100000000000002';
    document.body.appendChild(origin);
    origin.focus();

    let mounted: ReturnType<typeof render> | null = null;
    const onClose = vi.fn(() => mounted?.unmount());
    mounted = render(
      <ArtifactPanel refs={[v1]} artifactId={ID} version={1} fileKey="a100000000000002" originId={origin.id} onClose={onClose} />,
    );
    await flush();
    expect(screen.getByRole('dialog').contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(origin);
    origin.remove();
  });

  it('returns focus to the LATEST opening card when another card re-targets an open panel', async () => {
    const v1 = audit(1);
    serveVersions([v1]);
    const first = document.createElement('button');
    first.id = 'artifact-file-a100000000000001';
    const second = document.createElement('button');
    second.id = 'artifact-file-a100000000000002';
    document.body.append(first, second);
    first.focus();
    const view = render(
      <ArtifactPanel refs={[v1]} artifactId={ID} version={1} fileKey="a100000000000001" originId={first.id} onClose={vi.fn()} />,
    );
    await flush();
    view.rerender(
      <ArtifactPanel refs={[v1]} artifactId={ID} version={1} fileKey="a100000000000002" originId={second.id} onClose={vi.fn()} />,
    );
    await flush();
    expect(screen.getByTestId('artifact-panel-subtitle').textContent).toContain('CSV');
    view.unmount();
    expect(document.activeElement).toBe(second);
    first.remove();
    second.remove();
  });

  it('at 400 px is the full-screen sheet: Back is offered, Tab is trapped, every Escape is its own', async () => {
    viewportWidth(400);
    expect(SHEET_MAX_WIDTH).toBe(767);
    const v1 = audit(1);
    serveVersions([v1]);
    const onClose = vi.fn();
    const field = document.createElement('input');
    document.body.appendChild(field);
    render(
      <ArtifactPanel refs={[v1]} artifactId={ID} version={1} fileKey="a100000000000004" originId={null} onClose={onClose} />,
    );
    await flush();
    await flush();
    const dialog = screen.getByRole('dialog');
    // The sheet's classes: fixed and full-bleed below `md`, a column from it.
    expect(dialog.className).toMatch(/(^|\s)fixed(\s|$)/);
    expect(dialog.className).toMatch(/(^|\s)md:static(\s|$)/);
    expect(within(dialog).getByRole('button', { name: 'Back to the conversation' }).className).toMatch(/md:hidden/);
    const stops = Array.from(
      dialog.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),[tabindex]:not([tabindex="-1"])'),
    );
    stops[stops.length - 1].focus();
    fireEvent.keyDown(stops[stops.length - 1], { key: 'Tab' });
    expect(document.activeElement).toBe(stops[0]);
    fireEvent.keyDown(field, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    field.remove();
  });

  it('at 1024 px is the column: Tab leaves and a field outside keeps its Escape', async () => {
    viewportWidth(1024);
    const v1 = audit(1);
    serveVersions([v1]);
    const onClose = vi.fn();
    const field = document.createElement('input');
    document.body.appendChild(field);
    render(
      <ArtifactPanel refs={[v1]} artifactId={ID} version={1} fileKey="a100000000000004" originId={null} onClose={onClose} />,
    );
    await flush();
    await flush();
    fireEvent.keyDown(field, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
    field.remove();
  });
});
