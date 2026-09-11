/**
 * lib/artifacts: the URL builders, the vocabulary, and the poll.
 *
 * The poll is the part with a failure mode worth pinning: it must stop on
 * the first terminal status, stop the moment its signal aborts (an unmounted
 * card must not keep asking), back off 2 s → 10 s and no further, and treat
 * a 404 as a settled answer rather than something to retry.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  apiUrl,
  ArtifactRequestError,
  artifactUrls,
  cardDomId,
  fileCardDomId,
  fileDownloadUrl,
  fileExtent,
  fileKey,
  fileMatchesKey,
  filePreviewUrl,
  formatLabel,
  isArtifactId,
  isFileId,
  isPreviewable,
  isTerminal,
  legacyFileKey,
  nextDelay,
  pollJob,
  POLL_MAX_MS,
  POLL_MIN_MS,
  previewKindFor,
  primaryFile,
  reportFileUrl,
  stageLabel,
  statusLine,
} from '@/lib/artifacts';
import type { ArtifactFile, ArtifactJob, ArtifactRef } from '@/lib/types';

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const JOB = 'ffffffffffffffffffffffffffffffff';
const FILE_ID = '0123456789abcdef';

const ref = (over: Partial<ArtifactRef> = {}): ArtifactRef => ({
  artifact_id: ID,
  version: 1,
  job_id: JOB,
  title: 'Quarterly review',
  kind: 'document',
  status: 'completed',
  files: [],
  preview_kind: 'pages',
  preview_pages: 3,
  preview_url: `/artifacts/${ID}/v/1/preview`,
  thumbnail_url: `/artifacts/${ID}/v/1/preview/1.png?w=240`,
  warnings: [],
  created_at: '2026-09-11T10:00:00Z',
  operation: 'create',
  status_url: `/artifacts/jobs/${JOB}`,
  ...over,
});

describe('URLs', () => {
  it('prefixes a relative orchestrator path with /api and refuses anything else', () => {
    expect(apiUrl(`/artifacts/${ID}/v/1/file/pdf?disposition=attachment`)).toBe(
      `/api/artifacts/${ID}/v/1/file/pdf?disposition=attachment`,
    );
    expect(apiUrl('https://evil.example/x')).toBe('');
    expect(apiUrl('/reports/x.pdf')).toBe('');
    expect(apiUrl('')).toBe('');
  });

  it('builds every route the API defines', () => {
    expect(artifactUrls.list()).toBe('/api/artifacts');
    expect(artifactUrls.list('c 1')).toBe('/api/artifacts?conversation_id=c%201');
    expect(artifactUrls.artifact(ID)).toBe(`/api/artifacts/${ID}`);
    expect(artifactUrls.version(ID, 2)).toBe(`/api/artifacts/${ID}/v/2`);
    expect(artifactUrls.file(ID, 2, 'pptx')).toBe(`/api/artifacts/${ID}/v/2/file/pptx?disposition=attachment`);
    expect(artifactUrls.file(ID, 2, 'pdf', 'inline')).toBe(`/api/artifacts/${ID}/v/2/file/pdf?disposition=inline`);
    // CONTRACT-2 §1: csv is a format; §2: files by id, the zip, the grid.
    expect(artifactUrls.file(ID, 2, 'csv')).toBe(`/api/artifacts/${ID}/v/2/file/csv?disposition=attachment`);
    expect(artifactUrls.fileById(ID, 2, FILE_ID)).toBe(`/api/artifacts/${ID}/v/2/f/${FILE_ID}?disposition=attachment`);
    expect(artifactUrls.fileById(ID, 2, FILE_ID, 'inline')).toBe(`/api/artifacts/${ID}/v/2/f/${FILE_ID}?disposition=inline`);
    expect(artifactUrls.zip(ID, 2)).toBe(`/api/artifacts/${ID}/v/2/zip`);
    expect(artifactUrls.grid(ID, 2, { file: FILE_ID })).toBe(`/api/artifacts/${ID}/v/2/grid?file=${FILE_ID}`);
    expect(artifactUrls.grid(ID, 2, { file: FILE_ID, sheet: 'Q3 Results', offset: 200, limit: 200 })).toBe(
      `/api/artifacts/${ID}/v/2/grid?file=${FILE_ID}&sheet=Q3+Results&offset=200&limit=200`,
    );
    expect(artifactUrls.preview(ID, 2)).toBe(`/api/artifacts/${ID}/v/2/preview`);
    expect(artifactUrls.page(ID, 2, 4, 1400)).toBe(`/api/artifacts/${ID}/v/2/preview/4.png?w=1400`);
    expect(artifactUrls.page(ID, 2, 4, 240)).toBe(`/api/artifacts/${ID}/v/2/preview/4.png?w=240`);
    expect(artifactUrls.sheets(ID, 2)).toBe(`/api/artifacts/${ID}/v/2/sheets`);
    expect(artifactUrls.sheets(ID, 2, { sheet: 'Q3 Results', rows: 200, cols: 50 })).toBe(
      `/api/artifacts/${ID}/v/2/sheets?sheet=Q3+Results&rows=200&cols=50`,
    );
    expect(artifactUrls.job(JOB)).toBe(`/api/artifacts/jobs/${JOB}`);
    expect(artifactUrls.cancel(JOB)).toBe(`/api/artifacts/jobs/${JOB}/cancel`);
    expect(artifactUrls.retry(JOB)).toBe(`/api/artifacts/jobs/${JOB}/retry`);
    expect(artifactUrls.convert(ID)).toBe(`/api/artifacts/${ID}/convert`);
  });

  it('recognises an id exactly as the server mints it', () => {
    expect(isArtifactId(ID)).toBe(true);
    expect(isArtifactId(ID.toUpperCase())).toBe(false);
    expect(isArtifactId('x')).toBe(false);
    expect(isArtifactId(null)).toBe(false);
  });

  it('builds nothing from anything that is not an id — a file id, a format, a report name', () => {
    expect(isFileId(FILE_ID)).toBe(true);
    expect(isFileId(FILE_ID.toUpperCase())).toBe(false);
    expect(isFileId(FILE_ID.slice(0, 15))).toBe(false);
    expect(isFileId(`legacy:${FILE_ID}`)).toBe(false);
    expect(isFileId(ID)).toBe(false);
    expect(artifactUrls.fileById(ID, 1, `${FILE_ID}/../x`)).toBe('');
    expect(artifactUrls.fileById(ID, 1, ID)).toBe('');
    expect(artifactUrls.grid(ID, 1, { file: 'nope' })).toBe('');
    expect(artifactUrls.file(ID, 1, 'exe')).toBe('');
    expect(artifactUrls.file(ID, 1, 'zip')).toBe('');
    expect(artifactUrls.zip('not-an-id', 1)).toBe('');
    expect(reportFileUrl('brief v1.pdf')).toBe('/api/reports/brief%20v1.pdf');
    expect(reportFileUrl('../etc/passwd')).toBe('');
    expect(reportFileUrl('.hidden')).toBe('');
    expect(reportFileUrl('a%2fb.pdf')).toBe('');
  });

  it('gives a version group and a file card stable DOM ids', () => {
    expect(cardDomId(ID, 3)).toBe(`artifact-card-${ID}-v3`);
    expect(fileCardDomId(FILE_ID)).toBe(`artifact-file-${FILE_ID}`);
    // A legacy key holds a filename: reduced to id-safe characters.
    expect(fileCardDomId(`${ID}:1:pdf:a b.pdf`)).toBe(`artifact-file-${ID}_1_pdf_a_b_pdf`);
  });
});

describe('file identity (CONTRACT-2 §2)', () => {
  const contractFile = (over: Partial<ArtifactFile> = {}): ArtifactFile => ({
    file_id: FILE_ID,
    role: 'primary',
    format: 'xlsx',
    filename: 'budget-v1.xlsx',
    title: 'Budget',
    mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    size: 1,
    download_url: `/artifacts/${ID}/v/1/f/${FILE_ID}?disposition=attachment`,
    inline_url: `/artifacts/${ID}/v/1/f/${FILE_ID}?disposition=inline`,
    preview_url: `/artifacts/${ID}/v/1/grid?file=${FILE_ID}`,
    ...over,
  });
  const oldFile = (over: Partial<ArtifactFile> = {}): ArtifactFile => ({
    format: 'xlsx',
    filename: 'budget-v1.xlsx',
    mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    size: 1,
    download_url: `/artifacts/${ID}/v/1/file/xlsx?disposition=attachment`,
    inline_url: `/artifacts/${ID}/v/1/file/xlsx?disposition=inline`,
    ...over,
  });
  const workbook = ref({ kind: 'workbook', preview_kind: 'grid', preview_pages: 0 });

  it('keys a file by its id, and a file without one by artifact:version:format:filename', () => {
    expect(fileKey(workbook, contractFile())).toBe(FILE_ID);
    expect(fileKey(workbook, oldFile())).toBe(`${ID}:1:xlsx:budget-v1.xlsx`);
    expect(legacyFileKey(workbook, contractFile())).toBe(`${ID}:1:xlsx:budget-v1.xlsx`);
    // Two CSVs of one version: different ids, different keys.
    expect(fileKey(workbook, contractFile({ format: 'csv', file_id: 'aaaaaaaaaaaaaaaa' }))).not.toBe(
      fileKey(workbook, contractFile({ format: 'csv', file_id: 'bbbbbbbbbbbbbbbb' })),
    );
    // A file that gained an id still answers to the key its old ref had.
    expect(fileMatchesKey(workbook, contractFile(), `${ID}:1:xlsx:budget-v1.xlsx`)).toBe(true);
    expect(fileMatchesKey(workbook, contractFile(), FILE_ID)).toBe(true);
    expect(fileMatchesKey(workbook, contractFile(), 'other')).toBe(false);
  });

  it('downloads by id when there is one, by the /file/{format} alias when there is not, and by /api/reports for a legacy report', () => {
    expect(fileDownloadUrl(workbook, contractFile())).toBe(
      `/api/artifacts/${ID}/v/1/f/${FILE_ID}?disposition=attachment`,
    );
    expect(fileDownloadUrl(workbook, contractFile(), 'inline')).toBe(
      `/api/artifacts/${ID}/v/1/f/${FILE_ID}?disposition=inline`,
    );
    expect(fileDownloadUrl(workbook, oldFile())).toBe(`/api/artifacts/${ID}/v/1/file/xlsx?disposition=attachment`);
    expect(
      fileDownloadUrl(workbook, contractFile({ file_id: 'legacy:budget v1.xlsx', filename: 'budget v1.xlsx' })),
    ).toBe('/api/reports/budget%20v1.xlsx');
    // A download_url in a history row is never followed as it came.
    expect(fileDownloadUrl(workbook, oldFile({ download_url: 'https://evil.example/x' }))).toBe(
      `/api/artifacts/${ID}/v/1/file/xlsx?disposition=attachment`,
    );
  });

  it('previews pages for the print formats and the grid for the tabular ones, and nothing for the rest', () => {
    expect(previewKindFor({ format: 'pdf' })).toBe('pages');
    expect(previewKindFor({ format: 'docx' })).toBe('pages');
    expect(previewKindFor({ format: 'pptx' })).toBe('pages');
    expect(previewKindFor({ format: 'xlsx' })).toBe('grid');
    expect(previewKindFor({ format: 'csv' })).toBe('grid');
    expect(previewKindFor({ format: 'txt' })).toBe('none');

    expect(isPreviewable(contractFile(), workbook)).toBe(true);
    expect(isPreviewable(contractFile({ format: 'csv' }), workbook)).toBe(true);
    // The server said this file has no preview.
    expect(isPreviewable(contractFile({ preview_url: '' }), workbook)).toBe(false);
    // A legacy report file has no panel at all.
    expect(isPreviewable(contractFile({ file_id: 'legacy:x.xlsx' }), workbook)).toBe(false);
    // A ref from before per-file previews: the version's kind decides.
    expect(isPreviewable(oldFile(), workbook)).toBe(true);
    expect(isPreviewable(oldFile(), ref({ preview_kind: 'none', preview_pages: 0 }))).toBe(false);
    expect(isPreviewable(oldFile({ format: 'pdf' }), ref({ preview_kind: 'pages', preview_pages: 3 }))).toBe(true);
    expect(isPreviewable(oldFile({ format: 'pdf' }), ref({ preview_kind: 'pages', preview_pages: 0 }))).toBe(false);

    expect(filePreviewUrl(workbook, contractFile())).toBe(`/api/artifacts/${ID}/v/1/grid?file=${FILE_ID}`);
    expect(filePreviewUrl(workbook, oldFile())).toBe(`/api/artifacts/${ID}/v/1/sheets`);
    expect(filePreviewUrl(ref(), contractFile({ format: 'pdf', preview_url: `/artifacts/${ID}/v/1/preview` }))).toBe(
      `/api/artifacts/${ID}/v/1/preview`,
    );
    expect(filePreviewUrl(workbook, contractFile({ preview_url: '' }))).toBe('');
  });
});

describe('vocabulary', () => {
  it('knows exactly the four terminal statuses', () => {
    for (const s of ['completed', 'completed_with_warnings', 'failed', 'cancelled']) {
      expect(isTerminal(s)).toBe(true);
    }
    for (const s of ['queued', 'running', '', undefined, null]) {
      expect(isTerminal(s)).toBe(false);
    }
  });

  it("prefers the server's stage title, then the fixed table, then the raw word", () => {
    expect(stageLabel('compose', 'Drafting the executive summary')).toBe('Drafting the executive summary');
    expect(stageLabel('compose')).toBe('Writing the content');
    expect(stageLabel('new_stage')).toBe('new stage');
    expect(stageLabel(null)).toBe('Working');
  });

  it('writes a truthful status line with no percentage', () => {
    const running: ArtifactJob = {
      job_id: JOB,
      artifact_id: ID,
      version: 1,
      status: 'running',
      stage: 'render',
      stage_title: 'Building the files',
      progress: { detail: 'pdf', elapsed_s: 12 },
    };
    expect(statusLine(ref({ status: 'running' }), running)).toBe('Building the files — pdf');
    expect(statusLine(ref({ status: 'running' }), null)).toBe('Working…');
    expect(statusLine(ref({ status: 'queued' }))).toBe('Queued — waiting for a worker');
    expect(statusLine(ref({ status: 'completed' }))).toBe('Ready');
    expect(statusLine(ref({ status: 'completed_with_warnings', warnings: ['a', 'b'] }))).toBe(
      'Ready · 2 notes',
    );
    expect(statusLine(ref({ status: 'failed' }), { ...running, status: 'failed', error: 'The PDF renderer timed out.' })).toBe(
      'Failed — The PDF renderer timed out.',
    );
    expect(statusLine(ref({ status: 'cancelled' }))).toBe('Cancelled');
    for (const line of [statusLine(ref({ status: 'running' }), running), statusLine(ref({ status: 'queued' }))]) {
      expect(line).not.toMatch(/%/);
    }
  });

  it('describes a file by pages, slides, sheets, or rows and columns', () => {
    expect(fileExtent({ pages: 1 })).toBe('1 page');
    expect(fileExtent({ pages: 12 })).toBe('12 pages');
    expect(fileExtent({ slides: 8 })).toBe('8 slides');
    expect(fileExtent({ sheets: 2 })).toBe('2 sheets');
    expect(fileExtent({})).toBe('');
    // CONTRACT-2 §2: rows (data rows, header excluded) and columns.
    expect(fileExtent({ rows: 500, columns: 11 })).toBe('500 rows · 11 columns');
    expect(fileExtent({ rows: 1, columns: 1 })).toBe('1 row · 1 column');
    expect(fileExtent({ rows: 30 })).toBe('30 rows');
    // One sheet says nothing its rows do not; several are named.
    expect(fileExtent({ sheets: 1, rows: 30, columns: 11 })).toBe('30 rows · 11 columns');
    expect(fileExtent({ sheets: 3, rows: 500, columns: 11 })).toBe('3 sheets · 500 rows · 11 columns');
    expect(fileExtent({ sheets: 1 })).toBe('1 sheet');
    // `null` is "not counted", never zero.
    expect(fileExtent({ pages: null, rows: null, columns: null })).toBe('');
  });

  it('names a format the way a person does', () => {
    expect(formatLabel('csv')).toBe('CSV');
    expect(formatLabel('pdf')).toBe('PDF');
    expect(formatLabel('docx')).toBe('Word');
    expect(formatLabel('pptx')).toBe('PowerPoint');
    expect(formatLabel('xlsx')).toBe('Excel');
    expect(formatLabel('zip')).toBe('ZIP');
    expect(formatLabel('vtt')).toBe('VTT');
    expect(formatLabel('')).toBe('File');
  });

  it('picks the native format as the primary file', () => {
    const pdf = { format: 'pdf', filename: 'a.pdf', mime_type: 'application/pdf', size: 1, download_url: '', inline_url: '' };
    const pptx = { ...pdf, format: 'pptx', filename: 'a.pptx' };
    expect(primaryFile(ref({ kind: 'presentation', files: [pdf, pptx] }))?.format).toBe('pptx');
    expect(primaryFile(ref({ kind: 'document', files: [pdf] }))?.format).toBe('pdf');
    expect(primaryFile(ref({ files: [] }))).toBeUndefined();
  });
});

describe('pollJob', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const job = (status: string, extra: Partial<ArtifactJob> = {}): ArtifactJob => ({
    job_id: JOB,
    artifact_id: ID,
    version: 1,
    status,
    ...extra,
  });

  it('backs off 2 s → 10 s and never further', () => {
    const seen = [POLL_MIN_MS];
    for (let i = 0; i < 8; i += 1) seen.push(nextDelay(seen[seen.length - 1]));
    expect(seen.slice(0, 5)).toEqual([2000, 3000, 4500, 6750, 10000]);
    expect(Math.max(...seen)).toBe(POLL_MAX_MS);
  });

  it('stops on the first terminal status and reports every answer on the way', async () => {
    const answers = [job('queued'), job('running', { stage: 'compose' }), job('completed')];
    const fetcher = vi.fn(async () => answers.shift() ?? job('completed'));
    const updates: string[] = [];
    const done = pollJob(JOB, { fetcher, onUpdate: (j) => updates.push(j.status) });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetcher).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetcher).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(3000);
    expect(fetcher).toHaveBeenCalledTimes(3);
    const result = await done;
    expect(result?.status).toBe('completed');
    expect(updates).toEqual(['queued', 'running', 'completed']);
    // Terminal means terminal: no timer is left to fire another request.
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it('stops the moment the signal aborts, resolving null', async () => {
    const fetcher = vi.fn(async () => job('running'));
    const controller = new AbortController();
    const done = pollJob(JOB, { fetcher, signal: controller.signal });
    await vi.advanceTimersByTimeAsync(0);
    expect(fetcher).toHaveBeenCalledTimes(1);
    controller.abort();
    const result = await done;
    expect(result).toBeNull();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('treats a 404 as settled and does not ask again', async () => {
    const fetcher = vi.fn(async () => {
      throw new ArtifactRequestError(404, 'This file is no longer available.');
    });
    const done = pollJob(JOB, { fetcher });
    await expect(done).rejects.toBeInstanceOf(ArtifactRequestError);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('retries a transient failure with backoff, then gives up', async () => {
    const fetcher = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    });
    const done = pollJob(JOB, { fetcher, maxTransientFailures: 3 });
    // Attach the rejection handler now, before the timers run it to completion.
    const outcome = done.then(
      () => 'resolved',
      (e: unknown) => (e instanceof TypeError ? 'gave up' : 'other'),
    );
    await vi.advanceTimersByTimeAsync(0);
    expect(fetcher).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetcher).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(3000);
    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(await outcome).toBe('gave up');
  });
});
