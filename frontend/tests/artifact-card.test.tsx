// @vitest-environment jsdom
/**
 * The generated-file cards: ONE FileCard per file under a version header
 * (CONTRACT-2 §9, 2026-09-12). Until then this file pinned "one card per
 * version with a chip per file and a download disclosure"; those pins are
 * replaced here, each with the sentence of the contract that changed them.
 * What did not change and is still pinned: Open and Download are DISTINCT
 * controls, and the status line is the server's stage — never a percentage.
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ArtifactCard, versionText } from '@/components/artifacts/ArtifactCard';
import { ArtifactCards, groupArtifacts } from '@/components/artifacts/ArtifactCards';
import type { ArtifactFile, ArtifactJob, ArtifactRef } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** A class token — `\b` cannot bound `]`, so the boundary is whitespace or the string's ends. */
const MAX_W = /(^|\s)max-w-\[680px\](\s|$)/;
const W_FULL = /(^|\s)w-full(\s|$)/;

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const ID2 = 'b3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const JOB = 'ffffffffffffffffffffffffffffffff';

/** A file exactly as CONTRACT-2 §2 puts it on the wire. */
const file = (format: string, fileId: string, over: Partial<ArtifactFile> = {}): ArtifactFile => ({
  file_id: fileId,
  role: format === 'pptx' || format === 'docx' || format === 'xlsx' ? 'primary' : format === 'csv' ? 'data' : 'companion',
  format,
  filename: `quarterly-review-v1.${format}`,
  title: 'Quarterly review',
  mime_type: 'application/octet-stream',
  size: 123_456,
  download_url: `/artifacts/${ID}/v/1/f/${fileId}?disposition=attachment`,
  inline_url: `/artifacts/${ID}/v/1/f/${fileId}?disposition=inline`,
  preview_url:
    format === 'xlsx' || format === 'csv'
      ? `/artifacts/${ID}/v/1/grid?file=${fileId}`
      : `/artifacts/${ID}/v/1/preview`,
  ...over,
});

/** A file as a ref persisted BEFORE file ids carried it: no id, no role, /file/{format} URLs. */
const legacyFile = (format: string, over: Partial<ArtifactFile> = {}): ArtifactFile => ({
  format,
  filename: `quarterly-review-v1.${format}`,
  mime_type: 'application/octet-stream',
  size: 123_456,
  download_url: `/artifacts/${ID}/v/1/file/${format}?disposition=attachment`,
  inline_url: `/artifacts/${ID}/v/1/file/${format}?disposition=inline`,
  ...over,
});

const ref = (over: Partial<ArtifactRef> = {}): ArtifactRef => ({
  artifact_id: ID,
  version: 1,
  job_id: JOB,
  title: 'Quarterly review',
  kind: 'presentation',
  status: 'completed',
  files: [file('pptx', '0123456789abcdef', { slides: 12 }), file('pdf', 'fedcba9876543210', { pages: 12 })],
  preview_kind: 'pages',
  preview_pages: 12,
  preview_url: `/artifacts/${ID}/v/1/preview`,
  thumbnail_url: `/artifacts/${ID}/v/1/preview/1.png?w=240`,
  warnings: [],
  created_at: '2026-09-11T10:00:00Z',
  operation: 'create',
  status_url: `/artifacts/jobs/${JOB}`,
  download_all_url: `/artifacts/${ID}/v/1/zip`,
  package: { count: 2 },
  ...over,
});

describe('ArtifactCard — one FileCard per file under a version header', () => {
  it('renders the header (title, kind, "v1 · Created", ready status) and one card per file with its facts', () => {
    // CONTRACT-2 §9: "ONE FileCard per file … grouped per artifact version …
    // with a group header (title · version · status)". The old pin — one
    // card per version with a chip per file — is replaced.
    render(<ArtifactCard artifact={ref()} onOpen={vi.fn()} />);
    expect(screen.getByTitle('Quarterly review')).toBeTruthy();
    expect(screen.getByTestId('artifact-version').textContent).toBe('Presentation · v1 · Created');
    expect(screen.getByTestId('artifact-status').textContent).toContain('Ready');

    const cards = screen.getAllByTestId('file-card');
    expect(cards.length).toBe(2);
    expect(cards.map((c) => c.getAttribute('data-file-key'))).toEqual(['0123456789abcdef', 'fedcba9876543210']);
    expect(within(cards[0]).getByTestId('file-card-meta').textContent).toBe('PowerPoint · 121 KB · 12 slides');
    expect(within(cards[1]).getByTestId('file-card-meta').textContent).toBe('PDF · 121 KB · 12 pages');
    // The status is said once, in the header — not repeated per card.
    expect(screen.getAllByTestId('artifact-status').length).toBe(1);
    expect(screen.queryByTestId('file-card-status')).toBeNull();
  });

  it('renders a workbook of four files as four cards under one header, with "Download all"', () => {
    // CONTRACT-2 §1: a workbook may now be xlsx + csv + docx + pdf from one
    // spec; §9: a "Download all" link (download_all_url) when ≥ 2 files.
    render(
      <ArtifactCard
        artifact={ref({
          kind: 'workbook',
          title: 'IR Session Audit',
          files: [
            file('xlsx', '1111111111111111', { sheets: 1, rows: 30, columns: 11 }),
            file('csv', '2222222222222222', { rows: 30, columns: 11 }),
            file('docx', '3333333333333333', { pages: 3 }),
            file('pdf', '4444444444444444', { pages: 3 }),
          ],
          package: { count: 4 },
        })}
        onOpen={vi.fn()}
      />,
    );
    expect(screen.getAllByTestId('artifact-card').length).toBe(1);
    const cards = screen.getAllByTestId('file-card');
    expect(cards.length).toBe(4);
    expect(cards.map((c) => within(c).getByTestId('file-card-meta').textContent)).toEqual([
      'Excel · 121 KB · 30 rows · 11 columns',
      'CSV · 121 KB · 30 rows · 11 columns',
      'Word · 121 KB · 3 pages',
      'PDF · 121 KB · 3 pages',
    ]);
    const all = screen.getByRole('link', { name: 'Download all 4 files as ZIP' });
    expect(all.getAttribute('href')).toBe(`/api/artifacts/${ID}/v/1/zip`);
    expect(all.hasAttribute('download')).toBe(true);
  });

  it('offers no "Download all" for a single file, nor for a ref that carries no download_all_url', () => {
    const { unmount } = render(
      <ArtifactCard
        artifact={ref({ files: [file('pdf', 'fedcba9876543210', { pages: 2 })], package: { count: 1 } })}
        onOpen={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('artifact-download-all')).toBeNull();
    unmount();
    // Two files, but a ref persisted before the zip route existed.
    render(<ArtifactCard artifact={ref({ download_all_url: undefined, package: undefined })} onOpen={vi.fn()} />);
    expect(screen.getAllByTestId('file-card').length).toBe(2);
    expect(screen.queryByTestId('artifact-download-all')).toBeNull();
  });

  it('keys two CSVs of one version by their different file ids — no collision', () => {
    // CONTRACT-2 §2: a per-sheet CSV has its own file_id (sheet in the hash)
    // and its own title "<title> — <sheet name>".
    render(
      <ArtifactCard
        artifact={ref({
          kind: 'workbook',
          title: 'Budget',
          files: [
            file('csv', 'aaaaaaaaaaaaaaaa', { filename: 'budget-v1-summary.csv', title: 'Budget — Summary', rows: 12, columns: 4 }),
            file('csv', 'bbbbbbbbbbbbbbbb', { filename: 'budget-v1-detail.csv', title: 'Budget — Detail', rows: 500, columns: 9 }),
          ],
        })}
        onOpen={vi.fn()}
      />,
    );
    const cards = screen.getAllByTestId('file-card');
    expect(cards.length).toBe(2);
    const keys = cards.map((c) => c.getAttribute('data-file-key'));
    expect(new Set(keys).size).toBe(2);
    expect(keys).toEqual(['aaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbb']);
    expect(within(cards[0]).getByTitle('budget-v1-summary.csv').textContent).toBe('Budget — Summary');
    expect(within(cards[1]).getByTitle('budget-v1-detail.csv').textContent).toBe('Budget — Detail');
    expect(screen.getByRole('link', { name: 'Download budget-v1-summary.csv' }).getAttribute('href')).toBe(
      `/api/artifacts/${ID}/v/1/f/aaaaaaaaaaaaaaaa?disposition=attachment`,
    );
    expect(screen.getByRole('link', { name: 'Download budget-v1-detail.csv' }).getAttribute('href')).toBe(
      `/api/artifacts/${ID}/v/1/f/bbbbbbbbbbbbbbbb?disposition=attachment`,
    );
  });

  it('renders a ref persisted before file ids with legacy keys and /file/{format} hrefs', () => {
    // CONTRACT-2 §2: "legacyKey = artifact_id:version:format:filename" and
    // "/file/{format} URLs when file_id is absent".
    render(
      <ArtifactCard
        artifact={ref({
          files: [legacyFile('pptx', { slides: 12 }), legacyFile('pdf', { pages: 12 })],
          download_all_url: undefined,
          package: undefined,
        })}
        onOpen={vi.fn()}
      />,
    );
    const cards = screen.getAllByTestId('file-card');
    expect(cards.map((c) => c.getAttribute('data-file-key'))).toEqual([
      `${ID}:1:pptx:quarterly-review-v1.pptx`,
      `${ID}:1:pdf:quarterly-review-v1.pdf`,
    ]);
    expect(screen.getByRole('link', { name: 'Download quarterly-review-v1.pptx' }).getAttribute('href')).toBe(
      `/api/artifacts/${ID}/v/1/file/pptx?disposition=attachment`,
    );
    expect(screen.getByRole('link', { name: 'Download quarterly-review-v1.pdf' }).getAttribute('href')).toBe(
      `/api/artifacts/${ID}/v/1/file/pdf?disposition=attachment`,
    );
  });

  it('shows warnings once per group, in the header, once the version is terminal', () => {
    render(
      <ArtifactCard
        artifact={ref({ status: 'completed_with_warnings', warnings: ['Two bullets were shortened.'] })}
        onOpen={vi.fn()}
      />,
    );
    expect(screen.getAllByTestId('artifact-warnings').length).toBe(1);
    expect(screen.getByTestId('artifact-warnings').textContent).toContain('Two bullets were shortened.');
    expect(screen.getByTestId('artifact-status').textContent).toContain('Ready · 1 note');
    // Each card carries the small chip; the notes themselves are not repeated.
    expect(screen.getAllByTestId('file-card-status').map((c) => c.textContent)).toEqual(['With notes', 'With notes']);
  });

  it('names an edited version "v2 · Updated" and a converted one "v2 · Converted"', () => {
    // CONTRACT-2 §9: the group header says "v2 · Updated" or "Created"
    // (was "v2 · edited").
    expect(versionText({ version: 1, operation: 'create' })).toBe('v1 · Created');
    expect(versionText({ version: 2, operation: 'edit' })).toBe('v2 · Updated');
    expect(versionText({ version: 2, operation: 'convert' })).toBe('v2 · Converted');
    render(
      <ArtifactCard artifact={ref({ version: 2, operation: 'edit', parent_version: 1 })} onOpen={vi.fn()} />,
    );
    expect(screen.getByTestId('artifact-version').textContent).toBe('Presentation · v2 · Updated');
  });

  it('clamps the group to the assistant column: max-w-[680px] and full width below it', () => {
    render(<ArtifactCard artifact={ref()} onOpen={vi.fn()} />);
    const group = screen.getByTestId('artifact-card');
    expect(group.className).toMatch(MAX_W);
    expect(group.className).toMatch(W_FULL);
    for (const card of screen.getAllByTestId('file-card')) {
      expect(card.className).toMatch(MAX_W);
      expect(card.className).toMatch(W_FULL);
    }
  });
});

describe('ArtifactCard — while generating', () => {
  it('shows the current stage title from the job, no file cards and no download control, and never a percentage', () => {
    const job: ArtifactJob = {
      job_id: JOB,
      artifact_id: ID,
      version: 1,
      status: 'running',
      stage: 'render',
      stage_title: 'Building the files',
    };
    render(<ArtifactCard artifact={ref({ status: 'running', files: [] })} job={job} onOpen={vi.fn()} />);
    const status = screen.getByTestId('artifact-status');
    expect(status.textContent).toContain('Building the files');
    expect(status.textContent).not.toMatch(/%/);
    expect(screen.queryByTestId('file-card')).toBeNull();
    expect(screen.queryByTestId('artifact-download')).toBeNull();
    expect(screen.queryByTestId('artifact-download-all')).toBeNull();
    expect(screen.getByLabelText('Generating')).toBeTruthy();
  });

  it('names a failure with the server sentence and offers no files', () => {
    const job: ArtifactJob = {
      job_id: JOB,
      artifact_id: ID,
      version: 1,
      status: 'failed',
      error: 'The PDF renderer timed out.',
    };
    render(<ArtifactCard artifact={ref({ status: 'failed' })} job={job} onOpen={vi.fn()} />);
    expect(screen.getByTestId('artifact-status').textContent).toBe('Failed — The PDF renderer timed out.');
    // No files are offered for a version that was never published.
    expect(screen.queryByTestId('file-card')).toBeNull();
    expect(screen.queryByTestId('artifact-download')).toBeNull();
  });

  it('offers a way into the panel while there are no file cards: Progress while running, Details when failed', () => {
    // With one FileCard per file (CONTRACT-2 §9) a version without files
    // had nothing to click; the header control opens the panel's status
    // view with no file key.
    const onOpen = vi.fn();
    const running: ArtifactJob = { job_id: JOB, artifact_id: ID, version: 1, status: 'running', stage: 'compose', stage_title: 'Writing the content' };
    const { unmount } = render(<ArtifactCard artifact={ref({ status: 'running', files: [] })} job={running} onOpen={onOpen} />);
    const progress = screen.getByTestId('artifact-open-status');
    expect(progress.textContent).toBe('Progress');
    expect(progress.getAttribute('aria-label')).toMatch(/^Show progress of /);
    fireEvent.click(progress);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen.mock.calls[0][1]).toBe(progress.id);
    expect(onOpen.mock.calls[0][2]).toBe('');
    unmount();
    const failed: ArtifactJob = { job_id: JOB, artifact_id: ID, version: 1, status: 'failed', error: 'x' };
    render(<ArtifactCard artifact={ref({ status: 'failed' })} job={failed} onOpen={vi.fn()} />);
    expect(screen.getByTestId('artifact-open-status').textContent).toBe('Details');
  });

  it('shows no status control once the version has files', () => {
    render(<ArtifactCard artifact={ref({ status: 'completed' })} onOpen={vi.fn()} />);
    expect(screen.queryByTestId('artifact-open-status')).toBeNull();
    expect(screen.getAllByTestId('file-card').length).toBeGreaterThan(0);
  });
});

describe('ArtifactCard — Open and Download are distinct', () => {
  it('clicking a card body calls onOpen with the ref, the card control id and the file key, and navigates nowhere', () => {
    const onOpen = vi.fn();
    render(<ArtifactCard artifact={ref()} onOpen={onOpen} />);
    const open = screen.getByRole('button', { name: /Open Quarterly review \(PDF/ });
    expect(open.tagName).toBe('BUTTON');
    expect(open.getAttribute('href')).toBeNull();
    fireEvent.click(open);
    expect(onOpen).toHaveBeenCalledTimes(1);
    const [artifact, originId, key] = onOpen.mock.calls[0];
    expect(artifact.artifact_id).toBe(ID);
    expect(key).toBe('fedcba9876543210');
    expect(originId).toBe(open.id);
    expect(open.id).toBe('artifact-file-fedcba9876543210');
  });

  it('the download control is not inside the open button, points at disposition=attachment, and does not call onOpen', () => {
    const onOpen = vi.fn();
    render(<ArtifactCard artifact={ref()} onOpen={onOpen} />);
    const open = screen.getByRole('button', { name: /Open Quarterly review \(PowerPoint/ });
    const download = screen.getByRole('link', { name: 'Download quarterly-review-v1.pptx' });
    expect(open.contains(download)).toBe(false);
    expect(download.getAttribute('href')).toBe(
      `/api/artifacts/${ID}/v/1/f/0123456789abcdef?disposition=attachment`,
    );
    expect(download.getAttribute('download')).toBe('quarterly-review-v1.pptx');
    fireEvent.click(download);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('marks the file the panel is showing as current on its Open button, and only that one', () => {
    render(<ArtifactCard artifact={ref()} onOpen={vi.fn()} activeKey="fedcba9876543210" />);
    const pdf = screen.getByRole('button', { name: /Open Quarterly review \(PDF/ });
    const pptx = screen.getByRole('button', { name: /Open Quarterly review \(PowerPoint/ });
    // `aria-current`, not `aria-pressed`: Open is not a toggle.
    expect(pdf.getAttribute('aria-current')).toBe('true');
    expect(pdf.hasAttribute('aria-pressed')).toBe(false);
    expect(pptx.getAttribute('aria-current')).toBeNull();
    expect(pdf.closest('[data-testid="file-card"]')?.getAttribute('data-active')).toBe('true');
  });
});

describe('ArtifactCards — grouping', () => {
  it('groups versions by artifact in first-seen order, ascending within a group', () => {
    const groups = groupArtifacts([
      ref({ version: 2 }),
      ref({ artifact_id: ID2, title: 'Budget', kind: 'workbook' }),
      ref({ version: 1 }),
    ]);
    expect(groups.map((g) => g.map((r) => `${r.artifact_id.slice(0, 1)}:${r.version}`))).toEqual([
      ['a:1', 'a:2'],
      ['b:1'],
    ]);
  });

  it('renders one header per version, every file of every version, and hands Open the message siblings', () => {
    // CONTRACT-2 §9: one card per FILE, grouped per version, each version
    // with its own header (the old "title · N versions" heading is gone —
    // every version names itself now).
    const onOpen = vi.fn();
    render(
      <ArtifactCards
        artifacts={[ref({ version: 1 }), ref({ version: 2, operation: 'edit' })]}
        onOpen={onOpen}
        activeKey="fedcba9876543210"
      />,
    );
    expect(screen.getAllByTestId('artifact-card').length).toBe(2);
    expect(screen.getAllByTestId('file-card').length).toBe(4);
    expect(screen.getAllByTestId('artifact-version').map((p) => p.textContent)).toEqual([
      'Presentation · v1 · Created',
      'Presentation · v2 · Updated',
    ]);
    expect(screen.queryByText('Quarterly review · 2 versions')).toBeNull();
    // The section, too, is clamped to the column.
    expect(screen.getByTestId('artifact-cards').className).toMatch(MAX_W);

    fireEvent.click(screen.getAllByRole('button', { name: /Open Quarterly review \(PDF/ })[1]);
    expect(onOpen).toHaveBeenCalledTimes(1);
    const [artifact, , key, siblings] = onOpen.mock.calls[0];
    expect(artifact.version).toBe(2);
    expect(key).toBe('fedcba9876543210');
    expect(siblings.map((r: ArtifactRef) => r.version)).toEqual([1, 2]);
  });

  it('renders nothing for an empty list', () => {
    const { container } = render(<ArtifactCards artifacts={[]} onOpen={vi.fn()} />);
    expect(container.innerHTML).toBe('');
  });
});
