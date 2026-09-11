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
 * under-900 px sheet, where the panel is genuinely modal.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ArtifactPanel } from '@/components/artifacts/ArtifactPanel';
import type { ArtifactRef } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** The viewport the panel believes it is in: the sheet (<900 px) or the column. */
function viewport(mode: 'sheet' | 'desktop') {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: mode === 'sheet' && query === '(max-width: 899px)',
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
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
    // No preview for this kind: the empty state says so and points at Download.
    expect(screen.getByText('There is nothing to preview for this file.')).toBeTruthy();
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
