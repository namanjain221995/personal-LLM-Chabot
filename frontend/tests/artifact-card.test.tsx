// @vitest-environment jsdom
/**
 * The generated-file card: every field it promises is rendered, Open and
 * Download are DISTINCT controls, and the status line is the server's stage
 * — never a percentage.
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ArtifactCard } from '@/components/artifacts/ArtifactCard';
import { ArtifactCards, groupArtifacts } from '@/components/artifacts/ArtifactCards';
import type { ArtifactFile, ArtifactJob, ArtifactRef } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const ID2 = 'b3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const JOB = 'ffffffffffffffffffffffffffffffff';

const file = (format: string, over: Partial<ArtifactFile> = {}): ArtifactFile => ({
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
  files: [file('pptx', { slides: 12 }), file('pdf', { pages: 12 })],
  preview_kind: 'pages',
  preview_pages: 12,
  preview_url: `/artifacts/${ID}/v/1/preview`,
  thumbnail_url: `/artifacts/${ID}/v/1/preview/1.png?w=240`,
  warnings: [],
  created_at: '2026-09-11T10:00:00Z',
  operation: 'create',
  status_url: `/artifacts/jobs/${JOB}`,
  ...over,
});

describe('ArtifactCard — fields', () => {
  it('renders the title, kind, version, filename, one chip per file with size and extent, and a ready status', () => {
    render(<ArtifactCard artifact={ref()} onOpen={vi.fn()} />);
    expect(screen.getByTitle('Quarterly review')).toBeTruthy();
    expect(screen.getByText(/Presentation · v1/)).toBeTruthy();
    // The primary (native-format) filename sits under the title, inside the
    // Open button — the download menu repeats every name, so scope to it.
    const open = screen.getByRole('button', { name: /Open Quarterly review/ });
    expect(within(open).getByText('quarterly-review-v1.pptx')).toBeTruthy();
    // Chips: one badge per file with its extent and size.
    expect(within(open).getByText('12 slides')).toBeTruthy();
    expect(within(open).getByText('12 pages')).toBeTruthy();
    expect(within(open).getAllByText('121 KB').length).toBe(2);
    expect(within(open).getByText('PPT')).toBeTruthy();
    expect(within(open).getByText('PDF')).toBeTruthy();
    expect(screen.getByTestId('artifact-status').textContent).toContain('Ready');
  });

  it('shows warnings once the version is terminal', () => {
    render(
      <ArtifactCard
        artifact={ref({ status: 'completed_with_warnings', warnings: ['Two bullets were shortened.'] })}
        onOpen={vi.fn()}
      />,
    );
    expect(screen.getByTestId('artifact-warnings').textContent).toContain('Two bullets were shortened.');
    expect(screen.getByTestId('artifact-status').textContent).toContain('Ready · 1 note');
  });

  it('names an edited version as such', () => {
    render(
      <ArtifactCard artifact={ref({ version: 2, operation: 'edit', parent_version: 1 })} onOpen={vi.fn()} />,
    );
    expect(screen.getByText(/v2 · edited/)).toBeTruthy();
  });
});

describe('ArtifactCard — while generating', () => {
  it('shows the current stage title from the job and no download control, and never a percentage', () => {
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
    expect(screen.queryByTestId('artifact-download')).toBeNull();
    expect(screen.getByLabelText('Generating')).toBeTruthy();
  });

  it('names a failure with the server sentence', () => {
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
    expect(screen.queryByTestId('artifact-download')).toBeNull();
  });
});

describe('ArtifactCard — Open and Download are distinct', () => {
  it('clicking the card calls onOpen with the ref and its DOM id, and navigates nowhere', () => {
    const onOpen = vi.fn();
    render(<ArtifactCard artifact={ref()} onOpen={onOpen} />);
    const open = screen.getByRole('button', { name: /Open Quarterly review/ });
    expect(open.tagName).toBe('BUTTON');
    expect(open.getAttribute('href')).toBeNull();
    fireEvent.click(open);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen.mock.calls[0][0].artifact_id).toBe(ID);
    expect(onOpen.mock.calls[0][1]).toBe(`artifact-card-${ID}-v1`);
    expect(open.id).toBe(`artifact-card-${ID}-v1`);
  });

  it('the download control is not inside the open button and does not call onOpen', () => {
    const onOpen = vi.fn();
    render(<ArtifactCard artifact={ref()} onOpen={onOpen} />);
    const open = screen.getByRole('button', { name: /Open Quarterly review/ });
    const download = screen.getByTestId('artifact-download');
    expect(open.contains(download)).toBe(false);
    // Two files → a disclosure with one download link per format.
    fireEvent.click(within(download).getByText('Download'));
    const links = within(download).getAllByRole('link');
    expect(links.map((a) => a.getAttribute('href'))).toEqual([
      `/api/artifacts/${ID}/v/1/file/pptx?disposition=attachment`,
      `/api/artifacts/${ID}/v/1/file/pdf?disposition=attachment`,
    ]);
    expect(links.map((a) => a.getAttribute('download'))).toEqual([
      'quarterly-review-v1.pptx',
      'quarterly-review-v1.pdf',
    ]);
    fireEvent.click(links[0]);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('one file gets a plain download link with the file name as its accessible name', () => {
    const onOpen = vi.fn();
    render(<ArtifactCard artifact={ref({ kind: 'workbook', files: [file('xlsx', { sheets: 3 })] })} onOpen={onOpen} />);
    const link = screen.getByRole('link', { name: 'Download quarterly-review-v1.xlsx' });
    expect(link.getAttribute('href')).toBe(`/api/artifacts/${ID}/v/1/file/xlsx?disposition=attachment`);
    fireEvent.click(link);
    expect(onOpen).not.toHaveBeenCalled();
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

  it('renders one card per version and a group heading when there is more than one', () => {
    render(
      <ArtifactCards
        artifacts={[ref({ version: 1 }), ref({ version: 2, operation: 'edit' })]}
        onOpen={vi.fn()}
        activeKey={`${ID}:2`}
      />,
    );
    expect(screen.getAllByTestId('artifact-card').length).toBe(2);
    expect(screen.getByText('Quarterly review · 2 versions')).toBeTruthy();
    // The active version is marked on its Open button as CURRENT, not as a
    // pressed toggle — pressing Open again never closes the panel.
    const active = screen.getByRole('button', { name: /v2 · edited/ });
    expect(active.getAttribute('aria-current')).toBe('true');
    expect(active.hasAttribute('aria-pressed')).toBe(false);
    expect(screen.getByRole('button', { name: /v1\)/ }).getAttribute('aria-current')).toBeNull();
  });

  it('renders nothing for an empty list', () => {
    const { container } = render(<ArtifactCards artifacts={[]} onOpen={vi.fn()} />);
    expect(container.innerHTML).toBe('');
  });
});
