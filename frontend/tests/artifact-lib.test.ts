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
  fileExtent,
  isArtifactId,
  isTerminal,
  nextDelay,
  pollJob,
  POLL_MAX_MS,
  POLL_MIN_MS,
  primaryFile,
  stageLabel,
  statusLine,
} from '@/lib/artifacts';
import type { ArtifactJob, ArtifactRef } from '@/lib/types';

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const JOB = 'ffffffffffffffffffffffffffffffff';

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

  it('gives a card a stable DOM id per version', () => {
    expect(cardDomId(ID, 3)).toBe(`artifact-card-${ID}-v3`);
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

  it('describes a file by pages, slides or sheets', () => {
    expect(fileExtent({ pages: 1 })).toBe('1 page');
    expect(fileExtent({ pages: 12 })).toBe('12 pages');
    expect(fileExtent({ slides: 8 })).toBe('8 slides');
    expect(fileExtent({ sheets: 2 })).toBe('2 sheets');
    expect(fileExtent({})).toBe('');
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
