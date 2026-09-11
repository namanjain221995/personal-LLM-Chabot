/**
 * Artifact Studio, browser side (2026-09-11): URL builders, the two fetches
 * the cards and the panel need, and the poll that follows a job the chat
 * turn no longer covers.
 *
 * Why this exists as a module and not inside the components: every path here
 * is a RELATIVE orchestrator path (`/artifacts/<id>/v/<n>/...`) built by the
 * server from ids — docs/artifact-studio/CONTRACT.md §7 — and the browser's
 * only job is to prefix `/api` so it reaches the Next proxy at
 * app/api/artifacts/[[...path]]/route.ts. Keeping the prefixing in one place
 * means a card, a thumbnail and the page viewer cannot disagree about where
 * a file lives, and a test can pin the exact strings.
 *
 * The poll's backoff (2 s → 10 s, ×1.5) is the one the upload reconciler
 * settled on for the same question — "is the server done yet?" — and it
 * stops on the first terminal status or the caller's AbortSignal, whichever
 * comes first. A card that polled forever after its answer arrived would be
 * the same class of leak as a stream nobody closed.
 */

import type { ArtifactJob, ArtifactRef } from './types';

/* ------------------------------------------------------------------ paths */

/** Where the Next proxy for the orchestrator's `/artifacts` routes lives. */
export const ARTIFACT_API_PREFIX = '/api';

/**
 * Turn a relative orchestrator path from a ref (`/artifacts/...`) into the
 * browser-facing URL. Refuses anything that is not an artifact path — a ref
 * is data from the server, but a URL that pointed elsewhere must not be
 * followed because it happened to arrive in the right field.
 */
export function apiUrl(relative: string): string {
  if (typeof relative !== 'string' || !relative.startsWith('/artifacts/')) {
    return '';
  }
  return `${ARTIFACT_API_PREFIX}${relative}`;
}

const ID_RE = /^[a-f0-9]{32}$/;

/** uuid4 hex, exactly as the orchestrator mints artifact and job ids. */
export function isArtifactId(value: unknown): value is string {
  return typeof value === 'string' && ID_RE.test(value);
}

/**
 * Every URL is built from a VALIDATED id and an integer version — never from
 * a string that arrived in a history row. A ref whose id is not an id builds
 * an empty URL, which the fetch helpers treat as "nothing to load".
 */
function versionBase(artifactId: string, version: number): string {
  if (!isArtifactId(artifactId) || !Number.isFinite(version) || version < 1) return '';
  return `${ARTIFACT_API_PREFIX}/artifacts/${artifactId}/v/${Math.trunc(version)}`;
}

function jobBase(jobId: string): string {
  return isArtifactId(jobId) ? `${ARTIFACT_API_PREFIX}/artifacts/jobs/${jobId}` : '';
}

/** The API surface, as browser URLs (docs/artifact-studio/API.md). */
export const artifactUrls = {
  /** The caller's artifacts, newest first — one conversation's when given. */
  list: (conversationId?: string) =>
    `${ARTIFACT_API_PREFIX}/artifacts${
      conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : ''
    }`,
  artifact: (artifactId: string) =>
    isArtifactId(artifactId) ? `${ARTIFACT_API_PREFIX}/artifacts/${artifactId}` : '',
  version: (artifactId: string, version: number) => versionBase(artifactId, version),
  file: (
    artifactId: string,
    version: number,
    format: string,
    disposition: 'inline' | 'attachment' = 'attachment',
  ) => {
    const base = versionBase(artifactId, version);
    if (!base || !/^(pdf|docx|pptx|xlsx)$/.test(format)) return '';
    return `${base}/file/${format}?disposition=${disposition}`;
  },
  preview: (artifactId: string, version: number) => {
    const base = versionBase(artifactId, version);
    return base ? `${base}/preview` : '';
  },
  /** One rasterised page (1-based) at one of the two widths the server keeps. */
  page: (artifactId: string, version: number, page: number, width: 240 | 1400) => {
    const base = versionBase(artifactId, version);
    return base && page >= 1 ? `${base}/preview/${Math.trunc(page)}.png?w=${width}` : '';
  },
  sheets: (
    artifactId: string,
    version: number,
    opts: { sheet?: string; rows?: number; cols?: number } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.sheet) params.set('sheet', opts.sheet);
    if (opts.rows) params.set('rows', String(Math.trunc(opts.rows)));
    if (opts.cols) params.set('cols', String(Math.trunc(opts.cols)));
    const query = params.toString();
    const base = versionBase(artifactId, version);
    return base ? `${base}/sheets${query ? `?${query}` : ''}` : '';
  },
  job: (jobId: string) => jobBase(jobId),
  cancel: (jobId: string) => (jobBase(jobId) ? `${jobBase(jobId)}/cancel` : ''),
  retry: (jobId: string) => (jobBase(jobId) ? `${jobBase(jobId)}/retry` : ''),
  convert: (artifactId: string) =>
    isArtifactId(artifactId) ? `${ARTIFACT_API_PREFIX}/artifacts/${artifactId}/convert` : '',
} as const;

/* --------------------------------------------------------------- vocabulary */

/** Mirrors types.TERMINAL_STATUSES. */
export const TERMINAL_STATUSES: readonly string[] = [
  'completed',
  'completed_with_warnings',
  'failed',
  'cancelled',
];

export function isTerminal(status: string | undefined | null): boolean {
  return typeof status === 'string' && TERMINAL_STATUSES.includes(status);
}

/** Pipeline stages in order, with the server's titles (types.STAGE_TITLES). */
export const STAGES: readonly string[] = [
  'intent',
  'gather',
  'outline',
  'compose',
  'render',
  'validate',
  'preview',
];

export const STAGE_TITLES: Record<string, string> = {
  intent: 'Understanding the request',
  gather: 'Gathering sources',
  outline: 'Planning the document',
  compose: 'Writing the content',
  render: 'Building the files',
  validate: 'Checking the files',
  preview: 'Rendering the preview',
};

/**
 * The stage's title, preferring the server's own words when the job carries
 * them. A stage this build has never heard of is shown as it came — better
 * a raw word than a wrong sentence.
 */
export function stageLabel(stage?: string | null, stageTitle?: string | null): string {
  if (stageTitle && stageTitle.trim()) return stageTitle.trim();
  if (!stage) return 'Working';
  return STAGE_TITLES[stage] ?? stage.replace(/_/g, ' ');
}

export const STATUS_LABELS: Record<string, string> = {
  queued: 'Queued',
  running: 'Generating',
  completed: 'Ready',
  completed_with_warnings: 'Ready, with notes',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

export function statusLabel(status?: string | null): string {
  if (!status) return 'Unknown';
  return STATUS_LABELS[status] ?? status.replace(/_/g, ' ');
}

export const KIND_LABELS: Record<string, string> = {
  document: 'Document',
  presentation: 'Presentation',
  workbook: 'Workbook',
};

export function kindLabel(kind?: string | null): string {
  if (!kind) return 'File';
  return KIND_LABELS[kind] ?? kind.replace(/_/g, ' ');
}

/**
 * The one line under a card's title. While the job runs it names the STAGE
 * the server reports (no percentages — the pipeline has no way to know how
 * long "Writing the content" takes, so a number would be invented); once
 * terminal it names the outcome.
 */
export function statusLine(ref: ArtifactRef, job?: ArtifactJob | null): string {
  const status = job?.status ?? ref.status;
  if (status === 'queued') return 'Queued — waiting for a worker';
  if (status === 'running') {
    const stage = stageLabel(job?.stage, job?.stage_title);
    const detail = job?.progress?.detail?.trim();
    return detail ? `${stage} — ${detail}` : `${stage}…`;
  }
  if (status === 'failed') {
    const why = job?.error?.trim();
    return why ? `Failed — ${why}` : 'Failed';
  }
  if (status === 'cancelled') return 'Cancelled';
  if (status === 'completed_with_warnings') {
    const n = ref.warnings?.length ?? 0;
    return n ? `Ready · ${n} ${n === 1 ? 'note' : 'notes'}` : 'Ready';
  }
  if (status === 'completed') return 'Ready';
  return statusLabel(status);
}

/* ----------------------------------------------------------------- fetches */

/**
 * A non-2xx answer from the artifact API. `detail` is the server's own
 * sentence when it sent one ({detail: "..."}), otherwise a status-shaped
 * fallback — never the raw body of an unknown response.
 */
export class ArtifactRequestError extends Error {
  readonly status: number;
  readonly detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.name = 'ArtifactRequestError';
    this.status = status;
    this.detail = detail;
  }
}

function fallbackDetail(status: number): string {
  if (status === 401) return 'Sign in to open this file.';
  if (status === 403) return 'You do not have access to this file.';
  if (status === 404) return 'This file is no longer available.';
  if (status === 410) return 'This file was removed.';
  if (status >= 500) return 'The server could not answer right now.';
  return 'The request could not be completed.';
}

async function readDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as unknown;
    if (body && typeof body === 'object') {
      const detail = (body as { detail?: unknown; message?: unknown }).detail;
      if (typeof detail === 'string' && detail.trim()) return detail.trim();
      const message = (body as { message?: unknown }).message;
      if (typeof message === 'string' && message.trim()) return message.trim();
    }
  } catch {
    /* not JSON — the fallback below says enough */
  }
  return fallbackDetail(res.status);
}

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, {
    method: 'GET',
    credentials: 'same-origin',
    cache: 'no-store',
    headers: { accept: 'application/json' },
    signal,
  });
  if (!res.ok) throw new ArtifactRequestError(res.status, await readDetail(res));
  return (await res.json()) as T;
}

/** GET /api/artifacts/jobs/{job_id}. */
export function fetchJob(jobId: string, signal?: AbortSignal): Promise<ArtifactJob> {
  return getJson<ArtifactJob>(artifactUrls.job(jobId), signal);
}

/** What GET /artifacts/{id}/v/{n} answers: the ref, plus two side lists. */
export type ArtifactVersion = ArtifactRef & {
  validation?: unknown;
  assumptions?: string[];
};

/** GET /api/artifacts/{id}/v/{version}. */
export function fetchArtifact(
  artifactId: string,
  version: number,
  signal?: AbortSignal,
): Promise<ArtifactVersion> {
  return getJson<ArtifactVersion>(artifactUrls.version(artifactId, version), signal);
}

/** GET /artifacts/{id}/v/{n}/sheets — the workbook grid (API.md). */
export interface SheetsResponse {
  sheets: { name: string; rows: number; cols: number }[];
  /**
   * The requested (or first) sheet's window. `null` when the workbook has no
   * visible sheet at all — preview.sheet_grid answers `{sheets: [], sheet:
   * None}` for one — so the viewer must say so rather than read `.rows`.
   */
  sheet: {
    name: string;
    columns: string[];
    rows: unknown[][];
    truncated: boolean;
    /** "B12" → "=SUM(B2:B11)" — shown as text, never evaluated. */
    formulas?: Record<string, string>;
  } | null;
}

export function fetchSheets(
  artifactId: string,
  version: number,
  opts: { sheet?: string; rows?: number; cols?: number } = {},
  signal?: AbortSignal,
): Promise<SheetsResponse> {
  return getJson<SheetsResponse>(artifactUrls.sheets(artifactId, version, opts), signal);
}

/* ----------------------------------------------------------------- polling */

/** First wait after a non-terminal answer, and the ceiling the backoff hits. */
export const POLL_MIN_MS = 2_000;
export const POLL_MAX_MS = 10_000;

/** 2 s → 3 s → 4.5 s → 6.75 s → 10 s → 10 s … */
export function nextDelay(previous: number): number {
  return Math.min(POLL_MAX_MS, Math.round(previous * 1.5));
}

/**
 * Resolve after `ms`, or as soon as `signal` aborts (resolving, not
 * rejecting — the poll treats an abort as "stop quietly", not as an error).
 */
function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal?.removeEventListener('abort', done);
      resolve();
    }
    signal?.addEventListener('abort', done, { once: true });
  });
}

/**
 * Is this failure one that polling again could change? A dropped connection
 * or a 5xx is; a 401/403/404 is the server's settled answer about THIS id and
 * asking again only makes noise.
 */
function isTransient(err: unknown): boolean {
  if (err instanceof ArtifactRequestError) return err.status >= 500 || err.status === 429;
  // TypeError is what fetch throws for a network failure.
  return err instanceof TypeError;
}

export interface PollOptions {
  signal?: AbortSignal;
  /** Every answer, terminal or not — the card updates its stage line from these. */
  onUpdate?: (job: ArtifactJob) => void;
  /** Injected in tests; defaults to fetchJob. */
  fetcher?: (jobId: string, signal?: AbortSignal) => Promise<ArtifactJob>;
  /** Give up after this many consecutive transient failures (default 6). */
  maxTransientFailures?: number;
}

/**
 * Follow a job until it is terminal. Resolves with the terminal job, or with
 * null when the signal aborted first (the component unmounted, the panel
 * switched artifacts). Rejects only on a settled refusal (401/403/404) or
 * when transient failures exhaust their budget — both are things the caller
 * must SHOW, not retry.
 */
export async function pollJob(jobId: string, opts: PollOptions = {}): Promise<ArtifactJob | null> {
  const { signal, onUpdate, fetcher = fetchJob, maxTransientFailures = 6 } = opts;
  let delay = POLL_MIN_MS;
  let failures = 0;
  while (!signal?.aborted) {
    let job: ArtifactJob;
    try {
      job = await fetcher(jobId, signal);
      failures = 0;
    } catch (err) {
      if (signal?.aborted) return null;
      if (!isTransient(err)) throw err;
      failures += 1;
      if (failures >= maxTransientFailures) throw err;
      await wait(delay, signal);
      delay = nextDelay(delay);
      continue;
    }
    if (signal?.aborted) return null;
    onUpdate?.(job);
    if (isTerminal(job.status)) return job;
    await wait(delay, signal);
    delay = nextDelay(delay);
  }
  return null;
}

/* ------------------------------------------------------------- small helpers */

/** "3 pages" · "12 slides" · "2 sheets" — whichever the file carries. */
export function fileExtent(file: {
  pages?: number;
  slides?: number;
  sheets?: number;
}): string {
  if (typeof file.pages === 'number') return `${file.pages} ${file.pages === 1 ? 'page' : 'pages'}`;
  if (typeof file.slides === 'number') {
    return `${file.slides} ${file.slides === 1 ? 'slide' : 'slides'}`;
  }
  if (typeof file.sheets === 'number') {
    return `${file.sheets} ${file.sheets === 1 ? 'sheet' : 'sheets'}`;
  }
  return '';
}

/**
 * The file a "primary" action should reach for: the kind's native format
 * first (docx for a document, pptx for a deck, xlsx for a workbook), the PDF
 * next, then whatever is first.
 */
export function primaryFile(ref: ArtifactRef): ArtifactRef['files'][number] | undefined {
  const files = ref.files ?? [];
  const native =
    ref.kind === 'presentation' ? 'pptx' : ref.kind === 'workbook' ? 'xlsx' : 'docx';
  return (
    files.find((f) => f.format === native) ?? files.find((f) => f.format === 'pdf') ?? files[0]
  );
}

/** The DOM id a card carries, so the panel can hand focus back to it. */
export function cardDomId(artifactId: string, version: number): string {
  return `artifact-card-${artifactId}-v${Math.trunc(version)}`;
}
