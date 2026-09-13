// @vitest-environment jsdom
/**
 * Tap targets in the shared shell that the 2026-09-13 re-audit measured under
 * 32px on touch screens. jsdom lays nothing out, so these pin the classes the
 * browser measurement depended on. Measured in Chrome on a production build:
 * 32px at 360 and 390 (max-sm) and, with `[@media(pointer:coarse)]`, at 768
 * and 1024 with touch, where Close settings and Close search had been 23px,
 * Sign out 29.5px and the Copy chip 29.5px. A mouse keeps the compact sizes.
 */
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ArtifactCard } from '@/components/artifacts/ArtifactCard';
import { ArtifactPanel } from '@/components/artifacts/ArtifactPanel';
import { CopyButton } from '@/components/CopyButton';
import { Providers } from '@/components/Providers';
import { SearchPalette } from '@/components/SearchPalette';
import { SessionsSection } from '@/components/SecuritySettings';
import { SettingsDialog } from '@/components/SettingsDialog';
import type { ArtifactRef } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const classes = (el: Element) => (el.getAttribute('class') ?? '').split(/\s+/);
const noop = () => undefined;
const COARSE = '[@media(pointer:coarse)]';

describe('the Copy chip', () => {
  it('is 32px tall on a phone and on any touch screen, and stays compact under a mouse', () => {
    render(<CopyButton text="curl https://example.test" label="Copy code" />);
    const chip = screen.getByRole('button', { name: 'Copy code' });
    expect(classes(chip)).toEqual(expect.arrayContaining(['max-sm:min-h-8', `${COARSE}:min-h-8`, 'py-1']));
    expect(classes(chip)).not.toContain('min-h-8');
  });
});

describe('dialog close buttons on a touch tablet', () => {
  const touchBox = (el: HTMLElement) =>
    expect(classes(el)).toEqual(
      expect.arrayContaining([`${COARSE}:h-8`, `${COARSE}:w-8`, `${COARSE}:p-0`, 'max-sm:h-8', 'max-sm:w-8']),
    );

  it('makes Close settings a 32px box wherever the pointer is a finger', () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })));
    render(
      <Providers>
        <SettingsDialog open account={null} onClose={noop} />
      </Providers>,
    );
    touchBox(screen.getByRole('button', { name: 'Close settings' }));
  });

  it('makes Close search a 32px box wherever the pointer is a finger', () => {
    render(
      <SearchPalette open onClose={noop} recents={[]} onSelect={noop} onNewChat={noop} searchFn={async () => []} />,
    );
    touchBox(screen.getByRole('button', { name: 'Close search' }));
  });
});

describe('small row actions on a touch tablet', () => {
  it("gives a session's Sign out a 32px floor", async () => {
    const json = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as unknown as Response;
    const fetchFn = vi.fn(async () =>
      json({
        sessions: [
          { id: 's-other', current: false, created_at: '2026-08-30T10:00:00Z', last_seen_at: '2026-08-31T09:00:00Z', user_agent: 'Mozilla/5.0', ip: '10.0.0.9' },
          { id: 's-this', current: true, created_at: '2026-08-01T10:00:00Z', last_seen_at: '2026-09-01T08:00:00Z', user_agent: 'Mozilla/5.0', ip: '10.0.0.2' },
        ],
      }),
    );
    render(
      <Providers>
        <SessionsSection fetchFn={fetchFn} />
      </Providers>,
    );
    const signOut = await screen.findByRole('button', { name: 'Sign out' });
    expect(classes(signOut)).toEqual(expect.arrayContaining(['max-sm:min-h-8', `${COARSE}:min-h-8`]));
  });

  it("gives an artifact's Download all (ZIP) a 32px floor", () => {
    const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
    const JOB = 'ffffffffffffffffffffffffffffffff';
    const file = (format: string, fileId: string) => ({
      file_id: fileId,
      role: format === 'pptx' ? 'primary' : 'companion',
      format,
      filename: `review-v1.${format}`,
      title: 'Review',
      mime_type: 'application/octet-stream',
      size: 1234,
      download_url: `/artifacts/${ID}/v/1/f/${fileId}?disposition=attachment`,
      inline_url: `/artifacts/${ID}/v/1/f/${fileId}?disposition=inline`,
      preview_url: `/artifacts/${ID}/v/1/preview`,
    });
    const artifact = {
      artifact_id: ID,
      version: 1,
      job_id: JOB,
      title: 'Review',
      kind: 'presentation',
      status: 'completed',
      files: [file('pptx', '0123456789abcdef'), file('pdf', 'fedcba9876543210')],
      preview_kind: 'pages',
      preview_pages: 2,
      preview_url: `/artifacts/${ID}/v/1/preview`,
      warnings: [],
      created_at: '2026-09-11T10:00:00Z',
      operation: 'create',
      status_url: `/artifacts/jobs/${JOB}`,
      download_all_url: `/artifacts/${ID}/v/1/zip`,
      package: { count: 2 },
    } as unknown as ArtifactRef;
    render(<ArtifactCard artifact={artifact} onOpen={vi.fn()} />);
    const zip = screen.getByRole('link', { name: 'Download all 2 files as ZIP' });
    expect(classes(zip)).toEqual(expect.arrayContaining(['max-sm:min-h-8', `${COARSE}:min-h-8`]));
  });
});

describe('the file panel downloads on a touch tablet', () => {
  it('gives the header Download and the preview fallback a 32px floor wherever the pointer is a finger', async () => {
    // Measured before: the header link was 95.9x29.5 at 768 and 1024 with touch.
    const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
    const JOB = 'ffffffffffffffffffffffffffffffff';
    const version = {
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
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: noop,
      removeEventListener: noop,
      addListener: noop,
      removeListener: noop,
      dispatchEvent: () => false,
    }));
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => version })));
    render(<ArtifactPanel artifactId={ID} version={1} originId={null} onClose={noop} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const header = screen.getByTestId('artifact-panel-download');
    expect(classes(header)).toEqual(expect.arrayContaining(['max-sm:min-h-8', `${COARSE}:min-h-8`, 'py-1']));
    expect(classes(header)).not.toContain('min-h-8');
    const fallback = screen.getByTestId('artifact-panel-download-fallback');
    expect(classes(fallback)).toEqual(expect.arrayContaining(['max-sm:min-h-8', `${COARSE}:min-h-8`]));
  });
});
