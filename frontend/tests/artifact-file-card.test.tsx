// @vitest-environment jsdom
/**
 * FileCard — the ONE card component every generated file renders through
 * (CONTRACT-2 §9): an Artifact Studio file, a ref persisted before file
 * ids, and an older engine's `report_files` entry by way of the legacy
 * adapter. The card body opens; Download downloads; the two never cross.
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { FileCard, fileMetaLine } from '@/components/artifacts/FileCard';
import { legacyFormat, legacyTitle, toArtifactRef } from '@/components/artifacts/legacyAdapter';
import { FileCards } from '@/components/FileCards';
import { fileKey } from '@/lib/artifacts';
import type { ArtifactFile, ArtifactRef } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** A class token — `\b` cannot bound `]`, so the boundary is whitespace or the string's ends. */
const MAX_W = /(^|\s)max-w-\[680px\](\s|$)/;
const W_FULL = /(^|\s)w-full(\s|$)/;

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const JOB = 'ffffffffffffffffffffffffffffffff';
const FILE_ID = '0123456789abcdef';

const file = (over: Partial<ArtifactFile> = {}): ArtifactFile => ({
  file_id: FILE_ID,
  role: 'companion',
  format: 'pdf',
  filename: 'onboarding-sop-v1.pdf',
  title: 'Onboarding SOP',
  mime_type: 'application/pdf',
  size: 14_336,
  pages: 2,
  download_url: `/artifacts/${ID}/v/1/f/${FILE_ID}?disposition=attachment`,
  inline_url: `/artifacts/${ID}/v/1/f/${FILE_ID}?disposition=inline`,
  preview_url: `/artifacts/${ID}/v/1/preview`,
  ...over,
});

const ref = (over: Partial<ArtifactRef> = {}): ArtifactRef => ({
  artifact_id: ID,
  version: 1,
  job_id: JOB,
  title: 'Onboarding SOP',
  kind: 'document',
  status: 'completed',
  files: [file()],
  preview_kind: 'pages',
  preview_pages: 2,
  preview_url: `/artifacts/${ID}/v/1/preview`,
  thumbnail_url: `/artifacts/${ID}/v/1/preview/1.png?w=240`,
  warnings: [],
  created_at: '2026-09-11T10:00:00Z',
  operation: 'create',
  status_url: `/artifacts/jobs/${JOB}`,
  ...over,
});

describe('FileCard — layout', () => {
  it('shows the format mark, the title, and "PDF · 14 KB · 2 pages", clamped to the column', () => {
    render(<FileCard file={file()} artifactRef={ref()} />);
    const card = screen.getByTestId('file-card');
    expect(card.className).toMatch(MAX_W);
    expect(card.className).toMatch(W_FULL);
    expect(card.getAttribute('data-format')).toBe('pdf');
    expect(card.getAttribute('data-file-key')).toBe(FILE_ID);
    expect(screen.getByTitle('onboarding-sop-v1.pdf').textContent).toBe('Onboarding SOP');
    expect(screen.getByTestId('file-card-meta').textContent).toBe('PDF · 14 KB · 2 pages');
  });

  it('writes the facts line per format: rows and columns for a CSV, sheets for a workbook, slides for a deck', () => {
    expect(fileMetaLine(file({ format: 'csv', size: 51_200, pages: undefined, rows: 500, columns: 11 }))).toBe(
      'CSV · 50 KB · 500 rows · 11 columns',
    );
    expect(fileMetaLine(file({ format: 'xlsx', size: 2048, pages: undefined, sheets: 3 }))).toBe('Excel · 2.0 KB · 3 sheets');
    expect(fileMetaLine(file({ format: 'pptx', size: 1_048_576, pages: undefined, slides: 6 }))).toBe(
      'PowerPoint · 1.0 MB · 6 slides',
    );
    // Counts the server did not make are not said, and neither is a size it did not.
    expect(fileMetaLine(file({ format: 'docx', size: 0, pages: null }))).toBe('Word');
  });

  it('truncates a 120-character title and keeps the full name in the tooltip and the accessible name', () => {
    const long = `${'quarterly-review-for-the-board-of-directors-'.repeat(3)}final`; // 137 chars
    const name = `${long}.pdf`;
    render(<FileCard file={file({ filename: name, title: long })} artifactRef={ref()} />);
    const title = screen.getByTitle(name);
    expect(title.className).toMatch(/\btruncate\b/);
    expect(title.textContent).toBe(long);
    const open = screen.getByRole('button', { name: /^Open / });
    expect(open.getAttribute('aria-label')).toContain(long);
    expect(open.getAttribute('aria-label')).toContain(name);
    expect(screen.getByRole('link', { name: `Download ${name}` })).toBeTruthy();
  });

  it('falls back to the group title, then the ref title, when the file carries none', () => {
    const { unmount } = render(
      <FileCard file={file({ title: undefined })} artifactRef={ref()} groupTitle="Group title" />,
    );
    expect(screen.getByTitle('onboarding-sop-v1.pdf').textContent).toBe('Group title');
    unmount();
    render(<FileCard file={file({ title: '' })} artifactRef={ref({ title: 'Ref title' })} />);
    expect(screen.getByTitle('onboarding-sop-v1.pdf').textContent).toBe('Ref title');
  });

  it('shows a status chip only when the version is not plain completed', () => {
    const { unmount } = render(<FileCard file={file()} artifactRef={ref()} status="completed" />);
    expect(screen.queryByTestId('file-card-status')).toBeNull();
    unmount();
    render(<FileCard file={file()} artifactRef={ref()} status="completed_with_warnings" />);
    expect(screen.getByTestId('file-card-status').textContent).toBe('With notes');
  });
});

describe('FileCard — Open and Download never cross', () => {
  it('the body is a button that calls onOpen with the key and its own DOM id', () => {
    const onOpen = vi.fn();
    render(<FileCard file={file()} artifactRef={ref()} onOpen={onOpen} />);
    const open = screen.getByRole('button', { name: 'Open Onboarding SOP (PDF, onboarding-sop-v1.pdf)' });
    expect(open.getAttribute('href')).toBeNull();
    fireEvent.click(open);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith(FILE_ID, open.id);
    expect(open.id).toBe(`artifact-file-${FILE_ID}`);
  });

  it('opens on Enter and on Space from the keyboard', () => {
    const onOpen = vi.fn();
    render(<FileCard file={file()} artifactRef={ref()} onOpen={onOpen} />);
    const open = screen.getByRole('button', { name: /^Open / });
    fireEvent.keyDown(open, { key: 'Enter' });
    expect(onOpen).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(open, { key: ' ' });
    expect(onOpen).toHaveBeenCalledTimes(2);
    fireEvent.keyDown(open, { key: 'a' });
    expect(onOpen).toHaveBeenCalledTimes(2);
  });

  it('Download is a sibling link with disposition=attachment that never calls onOpen', () => {
    const onOpen = vi.fn();
    render(<FileCard file={file()} artifactRef={ref()} onOpen={onOpen} />);
    const open = screen.getByRole('button', { name: /^Open / });
    const download = screen.getByRole('link', { name: 'Download onboarding-sop-v1.pdf' });
    expect(open.contains(download)).toBe(false);
    expect(download.getAttribute('href')).toBe(`/api/artifacts/${ID}/v/1/f/${FILE_ID}?disposition=attachment`);
    expect(download.getAttribute('download')).toBe('onboarding-sop-v1.pdf');
    fireEvent.click(download);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('builds the /file/{format} URL and a legacy key for a file without an id', () => {
    const onOpen = vi.fn();
    const legacy = file({ file_id: undefined, role: undefined, title: undefined, preview_url: undefined });
    render(<FileCard file={legacy} artifactRef={ref({ files: [legacy] })} onOpen={onOpen} />);
    const card = screen.getByTestId('file-card');
    expect(card.getAttribute('data-file-key')).toBe(`${ID}:1:pdf:onboarding-sop-v1.pdf`);
    expect(screen.getByRole('link', { name: 'Download onboarding-sop-v1.pdf' }).getAttribute('href')).toBe(
      `/api/artifacts/${ID}/v/1/file/pdf?disposition=attachment`,
    );
    fireEvent.click(screen.getByRole('button', { name: /^Open / }));
    expect(onOpen).toHaveBeenCalledWith(`${ID}:1:pdf:onboarding-sop-v1.pdf`, expect.stringMatching(/^artifact-file-/));
  });
});

describe('FileCard — a file with nothing to preview', () => {
  it('says "Preview unavailable — download file" and makes the body the download when the server rendered no preview', () => {
    // The file exists (Download works); its preview stage failed, so the
    // ref says `preview_kind: 'none'` and the file's own preview_url is ''.
    const onOpen = vi.fn();
    const failedPreview = file({ preview_url: '' });
    render(
      <FileCard
        file={failedPreview}
        artifactRef={ref({ preview_kind: 'none', preview_pages: 0, files: [failedPreview] })}
        onOpen={onOpen}
      />,
    );
    expect(screen.getByTestId('file-card-preview-unavailable').textContent).toContain(
      'Preview unavailable — download file',
    );
    expect(screen.queryByRole('button', { name: /^Open / })).toBeNull();
    const body = screen.getByRole('link', { name: 'Download onboarding-sop-v1.pdf' });
    expect(body.getAttribute('href')).toBe(`/api/artifacts/${ID}/v/1/f/${FILE_ID}?disposition=attachment`);
    expect(body.getAttribute('download')).toBe('onboarding-sop-v1.pdf');
    // One link, not two: the body IS the download.
    expect(screen.getAllByRole('link').length).toBe(1);
    fireEvent.click(body);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('falls back to the version preview kind for a ref persisted before per-file previews', () => {
    const legacy = file({ file_id: undefined, preview_url: undefined });
    render(<FileCard file={legacy} artifactRef={ref({ preview_kind: 'none', preview_pages: 0 })} />);
    expect(screen.getByTestId('file-card-preview-unavailable')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });
});

describe('legacy report_files through the adapter', () => {
  const UUID_NAME = 'f2c39b35-b713-422d-aed9-1df901ebf62b-8f7f23eb-u11.transcript.vtt';

  it('derives format, title, key and URLs as the contract says, and nothing it cannot know', () => {
    // CONTRACT-2 §9: file_id "legacy:"+filename, role primary, urls
    // /api/reports/{filename}, no preview.
    expect(legacyFormat('report.PDF')).toBe('pdf');
    expect(legacyFormat('README', 'txt')).toBe('txt');
    expect(legacyTitle('pipeline-review-2026-07-22.docx')).toBe('pipeline-review-2026-07-22');
    const group = toArtifactRef(
      [
        { filename: 'pipeline-review-2026-07-22.docx', type: 'docx', size: 48_213 },
        { filename: 'pipeline-review-2026-07-22.pdf', type: 'pdf', size: 91_427 },
        { filename: 'export.csv', type: 'csv' },
      ],
      'm1',
    );
    expect(group.status).toBe('completed');
    expect(group.preview_kind).toBe('none');
    expect(group.download_all_url).toBeUndefined();
    expect(group.files.map((f) => f.file_id)).toEqual([
      'legacy:pipeline-review-2026-07-22.docx',
      'legacy:pipeline-review-2026-07-22.pdf',
      'legacy:export.csv',
    ]);
    expect(group.files.map((f) => f.role)).toEqual(['primary', 'primary', 'primary']);
    expect(group.files.map((f) => f.format)).toEqual(['docx', 'pdf', 'csv']);
    expect(group.files.map((f) => f.download_url)).toEqual([
      '/api/reports/pipeline-review-2026-07-22.docx',
      '/api/reports/pipeline-review-2026-07-22.pdf',
      '/api/reports/export.csv',
    ]);
    expect(group.files.every((f) => f.preview_url === '')).toBe(true);
    expect(fileKey(group, group.files[0])).toBe('legacy:pipeline-review-2026-07-22.docx');
    // No /artifacts URL can ever be built from the placeholder id.
    expect(group.artifact_id).toBe('legacy-m1');
  });

  it('renders the same FileCard, as a download, with no Open and no /artifacts URL', () => {
    render(<FileCards files={[{ filename: UUID_NAME, type: 'vtt', size: 46_000 }]} />);
    const cards = screen.getAllByTestId('file-card');
    expect(cards.length).toBe(1);
    expect(screen.queryByRole('button')).toBeNull();
    const link = screen.getByRole('link', { name: `Download ${UUID_NAME}` });
    expect(link.getAttribute('href')).toBe(`/api/reports/${encodeURIComponent(UUID_NAME)}`);
    expect(link.getAttribute('download')).toBe(UUID_NAME);
    expect(within(cards[0]).getByTestId('file-card-meta').textContent).toBe('VTT · 45 KB');
    expect(document.body.innerHTML).not.toContain('/api/artifacts/');
  });

  it('refuses a report name the proxy would refuse rather than building a URL for it', () => {
    const group = toArtifactRef([{ filename: '../etc/passwd', type: 'txt' }], 'm2');
    expect(group.files[0].download_url).toBe('');
    render(<FileCards files={[{ filename: '../etc/passwd', type: 'txt' }]} />);
    // Shown, but as no link at all — never an `<a href="">`.
    expect(screen.getByTestId('artifact-file-unlinked')).toBeTruthy();
    expect(screen.queryByRole('link')).toBeNull();
  });
});
