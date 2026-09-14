/**
 * The Files tab's half of the console contract: wire shapes, paths, the
 * chunked uploader and the processing-event follower.
 *
 * WHAT THIS MIRRORS (2026-09-13). The File, Upload and UploadPart objects are
 * the ones `orchestrator/app/publicapi/files/wire.py` renders for /v1, with
 * the `processing` section `apifiles/jobs.processing_view` builds. The console
 * reads them through its own session-authenticated routes under
 * /api/devplatform/projects/{project}/…, never through /v1: CONTRACT §1 keeps
 * the session and the API key on separate surfaces, and the playground's rule
 * stands here too — there is no path by which a key could be used from this
 * page.
 *
 * WHAT IT NEVER DOES.
 *  · Download bytes. Files design D6: no byte route rides a cookie. Derived
 *    outputs are listed by name and size only; the tab says which API call
 *    fetches them with a key.
 *  · Keep a filename, a digest or a byte of content in browser storage. The
 *    resume record is an upload id, a part size and an expiry, keyed by the
 *    project, the file's size and its modification time. A re-picked file is
 *    matched against the SERVER's filename and byte count before a single
 *    part is sent.
 *  · Render free text the server did not put in the File object's fixed
 *    vocabulary. Facts, stages and derived names are allowlisted again here,
 *    after the server's own allowlist — a fact the orchestrator ever adds by
 *    mistake (an engine name, a path) is not drawn.
 *  · Invent progress. A part counts when the server has ANSWERED for it, the
 *    rule `lib/uploadDocument.ts` set for the chat composer: a bar that
 *    crawls with bytes handed to fetch and then stalls at 90% is a lie the
 *    reader learns to distrust.
 *
 * Plain TypeScript, no React, so the uploader and the follower are tested as
 * state machines with a mocked fetch and an injected clock.
 */

import { handleSessionEnd } from '@/lib/auth';
import { nav } from '@/components/admin/nav';
import { SSEParser } from '@/lib/sse';
import { AdminApiError, CONSOLE_BASE, OFFLINE_MESSAGE, errorSentence } from './api';

// ---------------------------------------------------------------------------
// Wire shapes
// ---------------------------------------------------------------------------

/** `processing.stages[]` — a stage name from `sniff.STAGES_BY_KIND`, or `assemble`. */
export interface FileStage {
  name: string;
  status: 'pending' | 'running' | 'done' | 'skipped' | 'failed';
  percent?: number;
}

export type ProcessingState = 'queued' | 'processing' | 'processed' | 'failed';

export interface FileProcessing {
  state: ProcessingState;
  kind: string;
  stage: string;
  /** 1-based; 0 or null while the parts are still being assembled. */
  step: number | null;
  total_steps: number | null;
  percent: number | null;
  stages: FileStage[];
  queue_position: number | null;
  waited_for_capacity_s: number;
  /** Unix seconds. */
  started_at: number | null;
  finished_at: number | null;
  /** `message` is one of design §4.6's fixed sentences, never exception text. */
  error: { code: string; message: string } | null;
  facts: Record<string, unknown>;
  derived: string[];
}

/**
 * The File object (Files design §2.2). `status` stays inside the SDK literal
 * set; `deleted` only ever arrives as the last `file.failed` event of a file
 * removed while its stream was open.
 *
 * `derived_bytes` is not on the /v1 object: design §14.1 puts "derived size"
 * in the console table, so the console route adds it. Absent means the route
 * did not say, and the cell draws an em dash — never a zero.
 */
export interface ConsoleFile {
  id: string;
  object: 'file';
  bytes: number;
  /** Unix seconds. */
  created_at: number | null;
  filename: string;
  purpose: string;
  status: 'uploaded' | 'processed' | 'error' | 'deleted';
  status_details: string | null;
  /** Unix seconds; null means kept until deleted. */
  expires_at: number | null;
  mime_type?: string | null;
  processing: FileProcessing | null;
  derived_bytes?: number | null;
}

/** The public list object; the console route answers the same shape. */
export interface FileList {
  object: 'list';
  data: ConsoleFile[];
  has_more: boolean;
  first_id: string | null;
  last_id: string | null;
}

export interface UploadPart {
  id: string;
  object: 'upload.part';
  part_number: number;
  bytes: number;
  sha256: string | null;
}

/** The Upload object; with `parts` it is the resume view (design §2.13). */
export interface UploadObject {
  id: string;
  object: 'upload';
  bytes: number;
  filename: string;
  purpose: string;
  status: 'pending' | 'finalizing' | 'completed' | 'cancelled' | 'expired' | string;
  expires_at: number | null;
  file: ConsoleFile | null;
  part_max_bytes: number;
  max_parts: number;
  bytes_received: number;
  parts?: UploadPart[];
}

/** `apifiles.derived.list_names` — metadata only; the console never fetches the bytes. */
export interface DerivedOutput {
  name: string;
  bytes: number;
  content_type: string;
}

/** `schema.project_file_storage`; every number null when the route did not measure it. */
export interface ProjectStorage {
  files: number | null;
  bytes: number | null;
  derived_bytes: number | null;
  uploads_pending: number | null;
  uploads_pending_bytes: number | null;
}

// ---------------------------------------------------------------------------
// Paths and the BFF operations they need
// ---------------------------------------------------------------------------

const seg = encodeURIComponent;

/**
 * Every path the Files tab calls, spelled once (the paths.ts rule). Ids are
 * encoded even though the BFF refuses anything outside `[A-Za-z0-9_-]`.
 */
export const filesPaths = {
  files: (projectId: string) => `projects/${seg(projectId)}/files`,
  file: (projectId: string, fileId: string) =>
    `projects/${seg(projectId)}/files/${seg(fileId)}`,
  fileEvents: (projectId: string, fileId: string) =>
    `projects/${seg(projectId)}/files/${seg(fileId)}/events`,
  fileDerived: (projectId: string, fileId: string) =>
    `projects/${seg(projectId)}/files/${seg(fileId)}/derived`,
  storage: (projectId: string) => `projects/${seg(projectId)}/storage`,
  uploads: (projectId: string) => `projects/${seg(projectId)}/uploads`,
  upload: (projectId: string, uploadId: string) =>
    `projects/${seg(projectId)}/uploads/${seg(uploadId)}`,
  uploadPart: (projectId: string, uploadId: string, partNumber: number) =>
    `projects/${seg(projectId)}/uploads/${seg(uploadId)}/parts/${Math.trunc(partNumber)}`,
  completeUpload: (projectId: string, uploadId: string) =>
    `projects/${seg(projectId)}/uploads/${seg(uploadId)}/complete`,
  cancelUpload: (projectId: string, uploadId: string) =>
    `projects/${seg(projectId)}/uploads/${seg(uploadId)}/cancel`,
} as const;

/** The status filter's values: `processing.state`, which the list route filters on. */
export const FILE_STATUS_FILTERS = ['queued', 'processing', 'processed', 'failed'] as const;
export type FileStatusFilter = (typeof FILE_STATUS_FILTERS)[number];

/** `sniff.STAGES_BY_KIND`'s kinds a person can filter by (`unknown` is transient). */
export const FILE_KINDS = [
  'pdf',
  'document',
  'presentation',
  'text',
  'html',
  'spreadsheet',
  'tabular',
  'image',
  'audio',
  'video',
  'unsupported',
] as const;

export const FILES_PAGE_LIMIT = 50;

/**
 * One BFF operation the tab needs, in `app/api/devplatform/[...path]/route.ts`
 * vocabulary. `relay` says how the BFF must carry it: `json` through the
 * buffered proxy, `events` piped unbuffered (a GET SSE stream), `part` as a
 * raw body bounded by CONSOLE_PART_BYTES with `x-part-sha256` forwarded.
 *
 * The route file has another owner; this table is what its Files entries must
 * say, and the Files suite checks every path above against it.
 */
export interface FilesOperation {
  pattern: readonly string[];
  methods: readonly string[];
  query?: readonly string[];
  relay: 'json' | 'events' | 'part';
}

export const FILES_CONSOLE_OPERATIONS: readonly FilesOperation[] = [
  { pattern: ['projects', ':id', 'files'], methods: ['GET'], query: ['after', 'limit', 'status', 'kind'], relay: 'json' },
  { pattern: ['projects', ':id', 'files', ':id'], methods: ['GET', 'DELETE'], relay: 'json' },
  { pattern: ['projects', ':id', 'files', ':id', 'events'], methods: ['GET'], relay: 'events' },
  { pattern: ['projects', ':id', 'files', ':id', 'derived'], methods: ['GET'], relay: 'json' },
  { pattern: ['projects', ':id', 'storage'], methods: ['GET'], relay: 'json' },
  { pattern: ['projects', ':id', 'uploads'], methods: ['POST'], relay: 'json' },
  { pattern: ['projects', ':id', 'uploads', ':id'], methods: ['GET'], relay: 'json' },
  { pattern: ['projects', ':id', 'uploads', ':id', 'parts', ':n'], methods: ['PUT'], relay: 'part' },
  { pattern: ['projects', ':id', 'uploads', ':id', 'complete'], methods: ['POST'], relay: 'json' },
  { pattern: ['projects', ':id', 'uploads', ':id', 'cancel'], methods: ['POST'], relay: 'json' },
];

/** The operation a concrete console path and method would use, or null. */
export function filesOperationFor(path: string, method: string): FilesOperation | null {
  const [bare] = path.split('?') as [string];
  const parts = bare.split('/').map((p) => decodeURIComponent(p));
  for (const op of FILES_CONSOLE_OPERATIONS) {
    if (op.pattern.length !== parts.length || !op.methods.includes(method.toUpperCase())) continue;
    const same = op.pattern.every((segment, i) => {
      const actual = parts[i] as string;
      if (segment === ':id') return /^[A-Za-z0-9_-]{1,64}$/.test(actual);
      if (segment === ':n') return /^\d{1,5}$/.test(actual);
      return segment === actual;
    });
    if (same) return op;
  }
  return null;
}

/** The list query, in a fixed order, with empty filters left out. */
export function filesListQuery(opts: {
  status?: string;
  kind?: string;
  after?: string | null;
  limit?: number;
}): Record<string, string | number | undefined> {
  const status = (FILE_STATUS_FILTERS as readonly string[]).includes(opts.status ?? '')
    ? opts.status
    : undefined;
  const kind = (FILE_KINDS as readonly string[]).includes(opts.kind ?? '') ? opts.kind : undefined;
  return {
    limit: opts.limit ?? FILES_PAGE_LIMIT,
    status,
    kind,
    after: opts.after ?? undefined,
  };
}

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

export const KIND_LABEL: Record<string, string> = {
  pdf: 'PDF',
  document: 'Document',
  presentation: 'Presentation',
  text: 'Text',
  html: 'HTML',
  spreadsheet: 'Spreadsheet',
  tabular: 'Table data',
  image: 'Image',
  audio: 'Audio',
  video: 'Video',
  unsupported: 'Unsupported',
  unknown: 'Detecting',
};

export const STATUS_FILTER_LABEL: Record<FileStatusFilter, string> = {
  queued: 'Queued',
  processing: 'Processing',
  processed: 'Processed',
  failed: 'Failed',
};

/**
 * What each stage does, in words a developer reads without the design open.
 * Generic on purpose: no engine or model is named, because the stage vocabulary
 * is what the public API promises and the engines behind it are not.
 */
export const STAGE_LABEL: Record<string, string> = {
  assemble: 'Assemble uploaded parts',
  sniff: 'Detect the file type',
  text: 'Extract text',
  ocr: 'Read scanned pages',
  sheets: 'Read sheets',
  profile: 'Profile columns',
  decode: 'Decode the image',
  variants: 'Prepare image sizes',
  probe: 'Inspect the media',
  audio: 'Extract the audio track',
  transcript: 'Transcribe speech',
  frames: 'Sample frames',
  vision: 'Describe frames',
  fusion: 'Summarise',
  artifacts: 'Write outputs',
  chunk: 'Split into passages',
  index: 'Build the search index',
  finalize: 'Finish',
};

export function stageLabel(name: string): string {
  return STAGE_LABEL[name] ?? 'Processing step';
}

export type FileStatusKey =
  | 'assembling'
  | 'queued'
  | 'processing'
  | 'processed'
  | 'failed'
  | 'unsupported'
  | 'deleted';

export interface FileStatusView {
  key: FileStatusKey;
  label: string;
  /** 0–100 while there is a measured percent; null otherwise. */
  percent: number | null;
}

function clampPercent(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.max(0, Math.min(100, Math.round(value)))
    : null;
}

/**
 * The status badge's words (design §14.1: Queued, Processing n/N, Processed,
 * Failed, Unsupported), plus Assembling for a chunked upload whose parts are
 * still being joined and Deleted for a file removed while it was on screen.
 */
export function fileStatus(file: ConsoleFile): FileStatusView {
  if (file.status === 'deleted') return { key: 'deleted', label: 'Deleted', percent: null };
  const p = file.processing;
  if (!p) {
    if (file.status === 'processed') return { key: 'processed', label: 'Processed', percent: 100 };
    if (file.status === 'error') return { key: 'failed', label: 'Failed', percent: null };
    return { key: 'queued', label: 'Queued', percent: null };
  }
  if (p.state === 'processed') return { key: 'processed', label: 'Processed', percent: 100 };
  if (p.state === 'failed') {
    return p.error?.code === 'unsupported_file'
      ? { key: 'unsupported', label: 'Unsupported', percent: null }
      : { key: 'failed', label: 'Failed', percent: null };
  }
  if (p.state === 'queued') return { key: 'queued', label: 'Queued', percent: null };
  if (p.stage === 'assemble') {
    return { key: 'assembling', label: 'Assembling', percent: clampPercent(p.percent) };
  }
  const step = typeof p.step === 'number' && p.step > 0 ? p.step : null;
  const total = typeof p.total_steps === 'number' && p.total_steps > 0 ? p.total_steps : null;
  return {
    key: 'processing',
    label: step !== null && total !== null ? `Processing ${step}/${total}` : 'Processing',
    percent: clampPercent(p.percent),
  };
}

/** True once nothing more will happen to this file on its own. */
export function isTerminal(file: ConsoleFile): boolean {
  if (file.status === 'deleted') return true;
  const state = file.processing?.state;
  if (state) return state === 'processed' || state === 'failed';
  return file.status === 'processed' || file.status === 'error';
}

export function kindOf(file: ConsoleFile): string {
  return file.processing?.kind ?? 'unknown';
}

/**
 * The Kind cell's word. `unknown` is "Detecting" only while something is still
 * happening to the file; a file that failed before its type was known (a
 * failed assembly, a sniff error) stays unknown, and says so.
 */
export function kindLabel(file: ConsoleFile): string {
  const kind = kindOf(file);
  if (kind === 'unknown') return isTerminal(file) ? 'Unknown' : (KIND_LABEL.unknown as string);
  return KIND_LABEL[kind] ?? 'Unknown';
}

type FactFormat = 'count' | 'seconds' | 'bool' | 'fraction' | 'px' | 'code';

/**
 * The facts the tab draws, and how. This is `jobs._FACT_KEYS` plus the
 * indexing keys — and nothing else: a key outside this table is not rendered
 * whatever its value.
 */
const FACTS: readonly (readonly [string, string, FactFormat])[] = [
  ['pages', 'Pages', 'count'],
  ['text_pages', 'Pages with a text layer', 'count'],
  ['ocr_pages', 'Pages read by OCR', 'count'],
  ['ocr_skipped_pages', 'Pages over the OCR budget', 'count'],
  ['sections', 'Sections', 'count'],
  ['slides', 'Slides', 'count'],
  ['sheets', 'Sheets', 'count'],
  ['rows', 'Rows', 'count'],
  ['columns', 'Columns', 'count'],
  ['width', 'Width', 'px'],
  ['height', 'Height', 'px'],
  ['format', 'Format', 'code'],
  ['duration_s', 'Duration', 'seconds'],
  ['has_audio', 'Audio track', 'bool'],
  ['has_video', 'Video track', 'bool'],
  ['language', 'Language', 'code'],
  ['speech_fraction', 'Speech', 'fraction'],
  ['chars', 'Characters', 'count'],
  ['estimated_tokens', 'Estimated tokens', 'count'],
  ['chunks', 'Passages', 'count'],
  ['chunks_indexed', 'Passages indexed', 'count'],
  ['index_truncated', 'Index truncated', 'bool'],
];

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h} h ${m} min`;
  if (m > 0) return `${m} min ${s} s`;
  return `${s} s`;
}

export function fileFacts(file: ConsoleFile): { key: string; label: string; value: string }[] {
  const facts = file.processing?.facts;
  if (!facts || typeof facts !== 'object') return [];
  const out: { key: string; label: string; value: string }[] = [];
  for (const [key, label, format] of FACTS) {
    const raw = (facts as Record<string, unknown>)[key];
    let value: string | null = null;
    if (format === 'bool') {
      if (typeof raw === 'boolean') value = raw ? 'Yes' : 'No';
    } else if (format === 'code') {
      // The server validates these (`^[A-Z0-9]{2,12}$`, a language tag); the
      // same shape is required again before a string is drawn.
      if (typeof raw === 'string' && /^[A-Za-z0-9-]{2,12}$/.test(raw)) value = raw;
    } else if (typeof raw === 'number' && Number.isFinite(raw)) {
      if (format === 'count') value = Math.round(raw).toLocaleString();
      else if (format === 'px') value = `${Math.round(raw).toLocaleString()} px`;
      else if (format === 'seconds') value = formatDuration(raw);
      else if (format === 'fraction') value = `${Math.round(Math.max(0, Math.min(1, raw)) * 100)}%`;
    }
    if (value !== null) out.push({ key, label, value });
  }
  return out;
}

const DERIVED_NAME = /^[a-z0-9_.-]{1,40}$/;

/** A derived output's name in words; null for a name outside the closed set. */
export function derivedLabel(name: string): string | null {
  if (!DERIVED_NAME.test(name)) return null;
  const sheet = /^sheet-([1-9][0-9]{0,3})\.csv$/.exec(name);
  if (sheet) return `Sheet ${sheet[1]} as CSV`;
  const labels: Record<string, string> = {
    'text.txt': 'Extracted text',
    'pages.json': 'Page map',
    'profile.json': 'Column profile',
    'image.png': 'Normalised image',
    'transcript.txt': 'Transcript',
    'transcript.srt': 'Transcript (SRT subtitles)',
    'transcript.vtt': 'Transcript (WebVTT subtitles)',
    'transcript.json': 'Transcript with timings',
    'summary.md': 'Summary',
    'screen_text.txt': 'On-screen text',
    'screen_text.json': 'On-screen text with timings',
  };
  return labels[name] ?? null;
}

// ---------------------------------------------------------------------------
// Requests
// ---------------------------------------------------------------------------

/**
 * A console failure that also says whether trying again can help.
 *
 * It IS an AdminApiError, so `messageOf` and every panel's error handling read
 * it unchanged; the extra fields exist for the uploader, which must tell a
 * dropped connection (send the part again) from a refusal (surface the
 * sentence and stop).
 */
export class FilesRequestError extends AdminApiError {
  readonly code: string | null;
  readonly retryable: boolean;
  readonly retryAfterS: number | null;

  constructor(
    status: number,
    message: string,
    init: { code?: string | null; retryable: boolean; retryAfterS?: number | null },
  ) {
    super(status, message);
    this.name = 'FilesRequestError';
    this.code = init.code ?? null;
    this.retryable = init.retryable;
    this.retryAfterS = init.retryAfterS ?? null;
  }
}

function abortError(): Error {
  const err = new Error('The request was cancelled.');
  err.name = 'AbortError';
  return err;
}

export function isAbortError(err: unknown): boolean {
  return err instanceof Error && err.name === 'AbortError';
}

/**
 * Whether a failed answer can succeed if sent again.
 *
 * `x-should-retry` decides when the route relays it (wire.py sets it on every
 * Files code). Without it: a network failure, 408 and 429 are retried; a 409
 * or 503 is retried only when it carries a SHORT Retry-After — the busy
 * `complete` and "a previous copy is still being removed" both say 2 s, while
 * a full disk says 60 s and will not have emptied by the next attempt; a
 * 502/504 from the BFF itself carries no verdict about the request. Everything
 * else is a decision about the request, and a decision is not retried.
 */
export function retryableAnswer(status: number, shouldRetry: string | null, retryAfterS: number | null): boolean {
  if (shouldRetry === 'true') return true;
  if (shouldRetry === 'false') return false;
  if (status === 0 || status === 408 || status === 429 || status === 502 || status === 504) return true;
  if (status === 409) return retryAfterS !== null && retryAfterS <= 5;
  if (status === 503) return retryAfterS === null || retryAfterS <= 5;
  return false;
}

function codeOf(body: unknown): string | null {
  if (!body || typeof body !== 'object') return null;
  const envelope = (body as { error?: unknown }).error;
  if (envelope && typeof envelope === 'object') {
    const code = (envelope as { code?: unknown }).code;
    if (typeof code === 'string') return code;
  }
  const detail = (body as { detail?: unknown }).detail;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const code = (detail as { code?: unknown }).code;
    if (typeof code === 'string') return code;
  }
  return null;
}

async function failureOf(res: Response, fallback: string): Promise<FilesRequestError> {
  let message = fallback;
  let code: string | null = null;
  try {
    const body = (await res.json()) as unknown;
    message = errorSentence(body) ?? fallback;
    // FastAPI's `{detail: {message, code}}` form, which errorSentence does not read.
    const detail = (body as { detail?: { message?: unknown } } | null)?.detail;
    if (message === fallback && detail && typeof detail.message === 'string') message = detail.message;
    code = codeOf(body);
  } catch {
    // A proxy's HTML page: the status sentence stands.
  }
  const retryAfterRaw = res.headers.get('retry-after');
  const retryAfterS =
    retryAfterRaw !== null && /^\d{1,6}$/.test(retryAfterRaw.trim()) ? Number(retryAfterRaw.trim()) : null;
  return new FilesRequestError(res.status, message, {
    code,
    retryAfterS,
    retryable: retryableAnswer(res.status, res.headers.get('x-should-retry'), retryAfterS),
  });
}

/**
 * One console call. `consoleJson` with the answer's retry verdict kept: the
 * 401 rule is `handleSessionEnd`'s, called exactly as api.ts calls it, and the
 * offline sentence is the console's own.
 */
export async function filesRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${CONSOLE_BASE}${path}`, { cache: 'no-store', ...init });
  } catch (err) {
    if (init.signal?.aborted || isAbortError(err)) throw abortError();
    throw new FilesRequestError(0, OFFLINE_MESSAGE, { code: 'network', retryable: true });
  }
  if (res.status === 401) {
    void handleSessionEnd(undefined, fetch, nav);
    throw new FilesRequestError(401, 'Signed out.', { retryable: false });
  }
  if (!res.ok) throw await failureOf(res, 'Something went wrong. Try again.');
  if (res.status === 204) return undefined as T;
  try {
    return (await res.json()) as T;
  } catch {
    throw new FilesRequestError(res.status, 'The server answered with something the console cannot read.', {
      retryable: false,
    });
  }
}

function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return filesRequest<T>(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
}

// ---------------------------------------------------------------------------
// The chunked uploader
// ---------------------------------------------------------------------------

/**
 * The console's part size: 8 MiB.
 *
 * Smaller than the API's 64 MiB default for three reasons. Progress is counted
 * per ANSWERED part, so 8 MiB is the step the bar moves in; a dropped
 * connection costs at most one part to send again; and the BFF relays a part
 * as a bounded body, so this is also the most one request holds in the
 * frontend process (the Cloudflare wall, 100 MB, is far above it). The part
 * budget is the upload's `max_parts`: 8 MiB × 10,000 is 78 GiB, and a larger
 * file is refused with a sentence pointing at the Uploads API rather than sent
 * in parts the BFF would refuse.
 */
export const CONSOLE_PART_BYTES = 8 * 1024 * 1024;

/** Five tries per request, 0.5 s doubling to an 8 s cap, with jitter. */
export const UPLOAD_MAX_ATTEMPTS = 5;
const RETRY_BASE_MS = 500;
const RETRY_CAP_MS = 8000;

/**
 * The longest `Retry-After` the uploader waits out on its own. A busy
 * `complete` says 2 s and a rate limit a few; an answer asking for longer (a
 * day, from a misconfigured limiter) is not slept through behind a spinner —
 * the upload pauses with the wait in words, and Resume or the `online` event
 * continues it.
 */
export const MAX_AUTO_RETRY_AFTER_S = 30;

/** Answers that mean the request did not get through, rather than that the server asked for a pause. */
const CONNECTION_FAILURE = new Set([0, 408, 502, 504]);

export type UploadState =
  | 'preparing'
  | 'uploading'
  | 'retrying'
  | 'paused'
  | 'completing'
  | 'uploaded'
  | 'failed'
  | 'cancelled';

export interface UploadSnapshot {
  state: UploadState;
  filename: string;
  bytesTotal: number;
  /** Bytes in parts the server has answered for. */
  bytesConfirmed: number;
  partsDone: number;
  partsTotal: number;
  uploadId: string | null;
  /** True when this run continued an upload an earlier run started. */
  resumed: boolean;
  /** A sentence for the tray: the server's refusal, or why it paused. */
  message: string | null;
  /** Seconds until the next attempt, while `retrying`. */
  retryInS: number | null;
  /**
   * Why the uploader is waiting, while `retrying`: the request did not get
   * through (`connection`), or the server answered and asked for a pause
   * (`busy`, a 429 or a busy 409/503).
   */
  retryReason: 'connection' | 'busy' | null;
  /** The File the completed upload produced. */
  file: ConsoleFile | null;
}

/** The slice of Storage the resume record needs; every call may throw. */
export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** The browser's localStorage, or null where it cannot be reached (private windows, tests). */
export function browserStore(): KeyValueStore | null {
  try {
    return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null;
  } catch {
    return null;
  }
}

export interface UploaderDeps {
  sleep: (ms: number, signal: AbortSignal) => Promise<void>;
  random: () => number;
  /** Unix milliseconds. */
  now: () => number;
  store: KeyValueStore | null;
  /** Hex SHA-256 of a part, or null when the browser cannot hash here. */
  hash: (part: Blob) => Promise<string | null>;
  /** Overridable for tests; production uses CONSOLE_PART_BYTES. */
  partBytes: number;
  maxAttempts: number;
}

export function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError());
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/**
 * The part's SHA-256 for `X-Part-SHA256`, so a part corrupted between this tab
 * and the orchestrator is refused (`checksum_mismatch`) instead of assembled.
 * `crypto.subtle` exists only in a secure context; outside one the header is
 * omitted, which the route accepts — it checks only what it is given.
 */
export async function partSha256(part: Blob): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle || typeof part.arrayBuffer !== 'function') return null;
  try {
    const digest = await subtle.digest('SHA-256', await part.arrayBuffer());
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
  } catch {
    return null;
  }
}

const DEFAULT_DEPS: UploaderDeps = {
  sleep: abortableSleep,
  random: Math.random,
  now: () => Date.now(),
  store: null,
  hash: partSha256,
  partBytes: CONSOLE_PART_BYTES,
  maxAttempts: UPLOAD_MAX_ATTEMPTS,
};

/** The file the uploader sends: a browser File, or anything shaped like one. */
export interface UploadSource {
  name: string;
  size: number;
  type: string;
  lastModified: number;
  slice(start: number, end: number): Blob;
}

/**
 * One remembered upload: an id, a part size and the expiry the server last
 * reported. The expiry is a pruning hint and nothing more — the server's own
 * slides 24 h past every accepted part (design §7.3), so whether an upload can
 * be resumed is always the SERVER's answer, asked before a part is sent.
 */
interface ResumeRecord {
  upload_id: string;
  part_bytes: number;
  expires_at: number | null;
}

const RECORD_PREFIX = 'techsara.console.upload.v1';

/** Entries kept under one key; the oldest goes first. */
const MAX_RECORDS_PER_KEY = 8;

/**
 * How long past its last-reported expiry a record is dropped without asking.
 * The server never keeps a pending upload beyond created + 7 days, and a
 * reported expiry is never earlier than creation, so 8 days past it the
 * upload is certainly gone.
 */
export const RECORD_STALE_AFTER_S = 8 * 86400;

/** Upload statuses no one can send to or finish again. */
const DEAD_UPLOAD = new Set(['cancelled', 'expired']);

/** The tray's words for a cancel, while the server is still being told. */
export const CANCELLING_SENTENCE = 'Cancelled. Asking the server to drop the parts already sent…';
export const UNREACHABLE_CANCEL_SENTENCE =
  'Cancelled here, but the server could not be reached. The parts already sent stay on it until the upload expires.';
export const FINISHED_CANCEL_SENTENCE =
  'Cancelled here, but an earlier attempt had already finished this upload. Its file is in the list; delete it there.';

/**
 * Upload ids a RUNNING uploader on this page is using. Two picks of the same
 * file never send parts to one upload at the same time; the second starts its
 * own. Released when the run ends, whatever its state.
 */
const CLAIMED = new Set<string>();

/**
 * Where resumable uploads are remembered: the project, the byte count and the
 * modification time. NOT the name — the key and the value hold nothing a
 * person typed or a file contains. Two files can share a key (split archive
 * volumes often do), so the value is a list and each entry is matched to its
 * file by the server's filename.
 */
export function resumeKey(projectId: string, file: Pick<UploadSource, 'size' | 'lastModified'>): string {
  return `${RECORD_PREFIX}:${projectId}:${file.size}:${Math.trunc(file.lastModified)}`;
}

function invisibleCodePoint(cp: number): boolean {
  return (
    cp <= 0x1f ||
    (cp >= 0x7f && cp <= 0x9f) ||
    (cp >= 0x200b && cp <= 0x200f) ||
    (cp >= 0x2028 && cp <= 0x202e) ||
    (cp >= 0x2060 && cp <= 0x2069) ||
    cp === 0xfeff
  );
}

/**
 * The filename the server stores for a picked name: `wire.normalize_filename`
 * step for step (NFC, control and bidi characters removed, either separator,
 * basename, trimmed, blank → `upload`, 255 code points keeping an extension of
 * up to 16). A resumed upload is matched against THIS, or a name with a
 * backslash or a zero-width space would never resume.
 */
export function serverFilename(raw: string): string {
  let text = Array.from(String(raw ?? '').normalize('NFC'))
    .filter((ch) => !invisibleCodePoint(ch.codePointAt(0) ?? 0))
    .join('');
  text = text.replace(/\\/g, '/');
  text = text.slice(text.lastIndexOf('/') + 1).trim();
  if (text === '' || text === '.' || text === '..') return 'upload';
  const chars = Array.from(text);
  if (chars.length > 255) {
    const dot = chars.lastIndexOf('.');
    const ext = dot >= 0 ? chars.slice(dot + 1) : [];
    const stem = dot >= 0 ? chars.slice(0, dot) : [];
    text =
      dot >= 0 && ext.length > 0 && ext.length <= 16 && stem.length > 0
        ? `${stem.slice(0, 255 - ext.length - 1).join('')}.${ext.join('')}`
        : chars.slice(0, 255).join('');
  }
  return text;
}

/**
 * Drop every resume record in this browser that no server can still hold
 * (RECORD_STALE_AFTER_S past its last-reported expiry), whichever file it was
 * for. The uploader prunes a key when it reads it; this sweeps the keys of
 * files nobody picks again. Never throws; returns how many records went.
 */
export function pruneStaleResumeRecords(
  storage: Pick<Storage, 'length' | 'key' | 'getItem' | 'setItem' | 'removeItem'> | null,
  nowMs: number,
): number {
  if (!storage) return 0;
  let dropped = 0;
  try {
    const keys: string[] = [];
    for (let i = 0; i < storage.length; i += 1) {
      const key = storage.key(i);
      if (key !== null && key.startsWith(`${RECORD_PREFIX}:`)) keys.push(key);
    }
    for (const key of keys) {
      let entries: unknown[];
      try {
        const parsed = JSON.parse(storage.getItem(key) ?? '[]') as unknown;
        entries = Array.isArray(parsed) ? parsed : [parsed];
      } catch {
        storage.removeItem(key);
        dropped += 1;
        continue;
      }
      const kept = entries.filter((entry) => {
        const expires = (entry as { expires_at?: unknown } | null)?.expires_at;
        return typeof expires !== 'number' || (expires + RECORD_STALE_AFTER_S) * 1000 > nowMs;
      });
      dropped += entries.length - kept.length;
      if (kept.length === 0) storage.removeItem(key);
      else if (kept.length !== entries.length) storage.setItem(key, JSON.stringify(kept));
    }
  } catch {
    // Storage blocked: nothing to sweep.
  }
  return dropped;
}

/** The part size for a file: the console's step, or the server's ceiling when that is lower. */
export function partBytesFor(preferred: number, partMaxBytes: number): number {
  return Math.max(1, partMaxBytes > 0 ? Math.min(preferred, partMaxBytes) : preferred);
}

interface Session {
  uploadId: string;
  partBytes: number;
  partsTotal: number;
  have: Set<number>;
  /**
   * The server already finished this upload in an earlier run whose answer
   * was lost: `answer` is its Upload with the File, or null when the file has
   * to be fetched by sending `complete` again (which replays the result).
   */
  finished: { answer: UploadObject | null } | null;
}

/**
 * One file's upload through the console, resumable.
 *
 * `run()` starts it — or, when this browser remembers an upload of a file with
 * the same size and modification time in the same project, asks the server
 * about it and continues: the parts it holds are not sent again, and an upload
 * the server already completed is finished from the server's answer instead
 * of being sent a second time. A transient failure is retried (after asking
 * the server again what it has, because a part whose answer was lost may well
 * have landed); when the connection stays down the upload PAUSES with its
 * record kept, and `run()` continues it later, from this tab or after a
 * reload. A refusal fails with the server's sentence.
 */
export class FileUploader {
  readonly projectId: string;
  readonly file: UploadSource;
  private readonly deps: UploaderDeps;
  private readonly onChange: (snapshot: UploadSnapshot) => void;
  private controller: AbortController | null = null;
  private stopReason: 'cancel' | 'pause' | null = null;
  private running: Promise<UploadSnapshot> | null = null;
  private claimed: string | null = null;
  /**
   * The server upload this uploader is continuing, from the moment a run
   * chose or created it until it is forgotten. It outlives a run: a run that
   * pauses while it is still asking the server keeps it, so Cancel always
   * knows which upload to drop — `snapshot.uploadId` is only what the tray
   * shows.
   */
  private boundUploadId: string | null = null;
  private cancelling: Promise<void> | null = null;
  snapshot: UploadSnapshot;

  constructor(
    projectId: string,
    file: UploadSource,
    onChange: (snapshot: UploadSnapshot) => void,
    deps: Partial<UploaderDeps> = {},
  ) {
    this.projectId = projectId;
    this.file = file;
    this.onChange = onChange;
    this.deps = { ...DEFAULT_DEPS, ...deps };
    this.snapshot = {
      state: 'preparing',
      filename: file.name,
      bytesTotal: file.size,
      bytesConfirmed: 0,
      partsDone: 0,
      partsTotal: 0,
      uploadId: null,
      resumed: false,
      message: null,
      retryInS: null,
      retryReason: null,
      file: null,
    };
  }

  private set(patch: Partial<UploadSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    this.onChange(this.snapshot);
  }

  /** Start, or continue a paused upload. A second call while running joins the first. */
  run(): Promise<UploadSnapshot> {
    if (this.running) return this.running;
    const { state } = this.snapshot;
    if (state === 'uploaded' || state === 'cancelled' || state === 'completing') {
      return Promise.resolve(this.snapshot);
    }
    this.running = this.execute().finally(() => {
      this.running = null;
    });
    return this.running;
  }

  /** Stop sending and keep the record, so a later `run()` continues. */
  pause(): void {
    if (!this.running || this.snapshot.state === 'completing') return;
    this.stopReason = 'pause';
    this.controller?.abort();
  }

  /**
   * Stop, tell the server, and forget the record. Not offered once `complete`
   * was sent.
   *
   * The state is `cancelled` at once; the message then says what the server
   * was told. `null` means it dropped the parts (or held none). A sentence
   * means something is still there: the server could not be reached, or it
   * refused (an upload already completed cannot be cancelled), in its words.
   */
  cancel(): Promise<void> {
    // A second press joins the first: the server is told once.
    if (this.cancelling) return this.cancelling;
    const { state } = this.snapshot;
    if (state === 'completing' || state === 'uploaded' || state === 'cancelled') return Promise.resolve();
    this.cancelling = this.stopAndDiscard();
    return this.cancelling;
  }

  private async stopAndDiscard(): Promise<void> {
    this.stopReason = 'cancel';
    this.controller?.abort();
    if (this.running) await this.running.catch(() => undefined);
    // The run finished in the moment before the abort reached it: there is a
    // file now, and nothing left to cancel.
    if (this.snapshot.state === 'uploaded') return;
    const pending = this.boundUploadId !== null || this.records().some((r) => !CLAIMED.has(r.upload_id));
    this.set({
      state: 'cancelled',
      message: pending ? CANCELLING_SENTENCE : null,
      retryInS: null,
      retryReason: null,
    });
    if (!pending) return;
    const left = await this.discard();
    this.set({ message: left });
  }

  /**
   * After a cancel: forget this file's upload and have the server drop it.
   *
   * When the run was stopped before it chose an upload — still asking the
   * server about the remembered ones — each remembered upload is asked about
   * again here, and every one that is THIS file's goes: otherwise picking the
   * file again would silently continue the upload the person just cancelled.
   * One of another file with the same size and time is left alone. Returns
   * null when nothing is left on the server, or the sentence that says what is.
   */
  private async discard(): Promise<string | null> {
    const bound = this.boundUploadId;
    if (bound !== null) {
      this.forget(bound);
      return this.tellServerToCancel(bound);
    }
    let left: string | null = null;
    for (const record of this.records()) {
      if (CLAIMED.has(record.upload_id)) continue;
      // Held while it is asked about, so a re-pick cannot resume it meanwhile.
      CLAIMED.add(record.upload_id);
      try {
        let upload: UploadObject;
        try {
          upload = await filesRequest<UploadObject>(filesPaths.upload(this.projectId, record.upload_id));
        } catch (err) {
          if (err instanceof FilesRequestError && (err.retryable || err.status === 401)) {
            // Cannot tell whose it is: kept, and said.
            left = left ?? UNREACHABLE_CANCEL_SENTENCE;
          } else {
            this.forget(record.upload_id);
          }
          continue;
        }
        if (DEAD_UPLOAD.has(upload.status)) {
          this.forget(record.upload_id);
          continue;
        }
        if (!this.sameFile(upload)) continue;
        this.forget(record.upload_id);
        if (upload.status === 'pending') {
          left = (await this.tellServerToCancel(record.upload_id)) ?? left;
        } else if (upload.status === 'finalizing' || (upload.status === 'completed' && upload.file)) {
          left = left ?? FINISHED_CANCEL_SENTENCE;
        }
      } finally {
        CLAIMED.delete(record.upload_id);
      }
    }
    return left;
  }

  /**
   * Ask the server to drop an upload's parts. Null when it did (or holds no
   * such upload); otherwise the sentence for what is left. Callers that only
   * clean up after a refusal ignore the answer: an upload nobody sends parts
   * to expires on its own.
   */
  private async tellServerToCancel(uploadId: string): Promise<string | null> {
    try {
      await postJson(filesPaths.cancelUpload(this.projectId, uploadId), {});
      return null;
    } catch (err) {
      if (!(err instanceof FilesRequestError)) return UNREACHABLE_CANCEL_SENTENCE;
      if (err.status === 404) return null;
      // A decision about the upload (409: it is completed, or being completed)
      // is the server's to word; offline, signed out, busy or crashed is not one.
      const decided = err.status >= 400 && err.status < 500 && ![401, 408, 429].includes(err.status);
      return decided ? `Cancelled here, but the server kept the upload: ${err.message}` : UNREACHABLE_CANCEL_SENTENCE;
    }
  }

  private claim(uploadId: string): void {
    this.release();
    CLAIMED.add(uploadId);
    this.claimed = uploadId;
    this.boundUploadId = uploadId;
  }

  private release(): void {
    if (this.claimed !== null) CLAIMED.delete(this.claimed);
    this.claimed = null;
  }

  private async execute(): Promise<UploadSnapshot> {
    const controller = new AbortController();
    this.controller = controller;
    this.stopReason = null;
    const signal = controller.signal;
    this.set({ state: 'preparing', message: null, retryInS: null, resumed: false, uploadId: this.boundUploadId });
    try {
      const session = await this.openSession(signal);
      this.set({ state: 'uploading', uploadId: session.uploadId });
      let answer = session.finished?.answer ?? null;
      if (!answer) {
        if (!session.finished) await this.sendMissing(session, signal);
        this.set({ state: 'completing', retryInS: null });
        answer = await this.withRetries(
          () => postJson<UploadObject>(filesPaths.completeUpload(this.projectId, session.uploadId), {}, signal),
          signal,
          session,
          true,
        );
      }
      this.forget(session.uploadId);
      this.set({ state: 'uploaded', file: answer?.file ?? null, message: null, retryInS: null });
    } catch (err) {
      if (isAbortError(err) || signal.aborted) {
        if (this.stopReason === 'pause') {
          this.set({ state: 'paused', message: 'Paused. The parts already sent are kept.', retryInS: null });
        }
        // A cancel sets its own state once the run has unwound.
      } else if (err instanceof FilesRequestError && err.retryable) {
        this.set({ state: 'paused', retryInS: null, message: this.pausedSentence(err) });
      } else {
        const message =
          err instanceof AdminApiError && err.message ? err.message : 'The upload could not be completed.';
        const status = err instanceof AdminApiError ? err.status : 0;
        // A 4xx is a decision about this upload that sending again will not
        // change (a refusal, a missing permission, a part the relay will not
        // carry), so the record goes and the server is told to drop the
        // parts. A 401 is the session, not the upload: signing in again and
        // re-picking the file continues it. A 5xx other than the retryable
        // ones may be a crash the upload survived, so Try again resumes it.
        if (status >= 400 && status < 500 && status !== 401) {
          const uploadId = this.boundUploadId;
          this.forget(uploadId);
          if (uploadId && status !== 404) void this.tellServerToCancel(uploadId);
        }
        this.set({ state: 'failed', message, retryInS: null });
      }
    } finally {
      if (this.controller === controller) this.controller = null;
      this.release();
    }
    return this.snapshot;
  }

  /** Why a run paused with its parts kept, and how to continue. */
  private pausedSentence(err: FilesRequestError): string {
    const { partsDone, partsTotal } = this.snapshot;
    if (CONNECTION_FAILURE.has(err.status)) {
      return partsTotal > 0
        ? `The connection dropped. ${partsDone} of ${partsTotal} parts are safe on the server; resume to send the rest.`
        : 'The connection dropped before the upload started. Resume to try again.';
    }
    const wait = err.retryAfterS !== null ? ` and asked for ${formatDuration(err.retryAfterS)} before the next request` : '';
    return partsTotal > 0
      ? `The server is busy${wait}. ${partsDone} of ${partsTotal} parts are safe on the server; resume to send the rest.`
      : `The server is busy${wait}. Resume to try again.`;
  }

  private backoffMs(attempt: number): number {
    const window = Math.min(RETRY_CAP_MS, RETRY_BASE_MS * 2 ** (attempt - 1));
    return Math.round(window * (0.5 + this.deps.random() * 0.5));
  }

  /**
   * Run `send` until it succeeds, a refusal ends it, or the attempts run out.
   * Between attempts the server is asked what it holds, so a part that landed
   * without its answer is not sent twice (`send` checks `session.have`).
   */
  private async withRetries<T>(
    send: () => Promise<T>,
    signal: AbortSignal,
    session: Session | null,
    completing = false,
  ): Promise<T> {
    for (let attempt = 1; ; attempt += 1) {
      try {
        return await send();
      } catch (err) {
        if (isAbortError(err) || signal.aborted) throw err;
        if (!(err instanceof FilesRequestError) || !err.retryable || attempt >= this.deps.maxAttempts) throw err;
        // A long wait is the person's to take, not a spinner's: pause instead.
        if (err.retryAfterS !== null && err.retryAfterS > MAX_AUTO_RETRY_AFTER_S) throw err;
        const waitMs = err.retryAfterS !== null ? err.retryAfterS * 1000 : this.backoffMs(attempt);
        this.set({
          state: 'retrying',
          retryInS: Math.max(1, Math.ceil(waitMs / 1000)),
          retryReason: CONNECTION_FAILURE.has(err.status) ? 'connection' : 'busy',
        });
        await this.deps.sleep(waitMs, signal);
        if (session && !completing) {
          try {
            await this.sync(session, signal);
          } catch (syncErr) {
            // Still offline: the next attempt says so. A refusal (the upload
            // was cancelled or expired meanwhile) ends the run now.
            if (!(syncErr instanceof FilesRequestError) || !syncErr.retryable) throw syncErr;
          }
        }
        this.set({ state: completing ? 'completing' : 'uploading', retryInS: null });
      }
    }
  }

  /** Every record under this file's key, stale ones pruned from storage as they are read. */
  private records(): ResumeRecord[] {
    const store = this.deps.store;
    if (!store) return [];
    try {
      const raw = store.getItem(resumeKey(this.projectId, this.file));
      if (!raw) return [];
      const parsed = JSON.parse(raw) as unknown;
      const entries = Array.isArray(parsed) ? parsed : [parsed];
      const nowS = this.deps.now() / 1000;
      const kept: ResumeRecord[] = [];
      for (const entry of entries) {
        const value = (entry ?? {}) as Partial<ResumeRecord>;
        if (typeof value.upload_id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(value.upload_id)) continue;
        if (typeof value.part_bytes !== 'number' || !(value.part_bytes > 0)) continue;
        const expires = typeof value.expires_at === 'number' ? value.expires_at : null;
        if (expires !== null && expires + RECORD_STALE_AFTER_S <= nowS) continue;
        kept.push({ upload_id: value.upload_id, part_bytes: value.part_bytes, expires_at: expires });
      }
      if (kept.length !== entries.length) this.write(kept);
      return kept;
    } catch {
      return [];
    }
  }

  private write(records: ResumeRecord[]): void {
    const store = this.deps.store;
    if (!store) return;
    const key = resumeKey(this.projectId, this.file);
    try {
      if (records.length === 0) store.removeItem(key);
      else store.setItem(key, JSON.stringify(records.slice(-MAX_RECORDS_PER_KEY)));
    } catch {
      // Storage full or blocked: the upload still runs, it just cannot survive a reload.
    }
  }

  private remember(record: ResumeRecord): void {
    this.write([...this.records().filter((r) => r.upload_id !== record.upload_id), record]);
  }

  /** Forget THIS upload's record only; another file under the same key keeps its own. */
  private forget(uploadId: string | null): void {
    if (!uploadId) return;
    if (this.boundUploadId === uploadId) this.boundUploadId = null;
    const records = this.records();
    const kept = records.filter((r) => r.upload_id !== uploadId);
    if (kept.length !== records.length) this.write(kept);
  }

  private confirmed(session: Session): void {
    let bytes = 0;
    for (const n of session.have) {
      const start = n * session.partBytes;
      bytes += Math.max(0, Math.min(this.file.size, start + session.partBytes) - start);
    }
    this.set({ partsDone: session.have.size, partsTotal: session.partsTotal, bytesConfirmed: bytes });
  }

  /** The part numbers the server holds whole, from the resume view. */
  private partsHeld(upload: UploadObject, session: Pick<Session, 'partBytes' | 'partsTotal'>): Set<number> {
    const have = new Set<number>();
    for (const part of upload.parts ?? []) {
      const n = part.part_number;
      if (!Number.isInteger(n) || n < 0 || n >= session.partsTotal) continue;
      const expected = Math.min(this.file.size, (n + 1) * session.partBytes) - n * session.partBytes;
      if (part.bytes === expected) have.add(n);
    }
    return have;
  }

  private async sync(session: Session, signal: AbortSignal): Promise<void> {
    const upload = await filesRequest<UploadObject>(filesPaths.upload(this.projectId, session.uploadId), { signal });
    if (upload.status !== 'pending') {
      throw new FilesRequestError(409, `This upload is ${upload.status} and no longer takes parts. Upload the file again.`, {
        code: 'upload_state_conflict',
        retryable: false,
      });
    }
    // The server's expiry slid with the parts it accepted; the record follows it.
    this.remember({ upload_id: session.uploadId, part_bytes: session.partBytes, expires_at: upload.expires_at });
    for (const n of this.partsHeld(upload, session)) session.have.add(n);
    this.confirmed(session);
  }

  private sameFile(upload: UploadObject): boolean {
    return upload.bytes === this.file.size && upload.filename === serverFilename(this.file.name);
  }

  private async openSession(signal: AbortSignal): Promise<Session> {
    // The upload an earlier run of THIS uploader was continuing goes first;
    // then newest first, since the upload most recently started for this key
    // is the likeliest to be this file's.
    const remembered = this.records().reverse();
    const bound = remembered.filter((r) => r.upload_id === this.boundUploadId);
    for (const record of [...bound, ...remembered.filter((r) => r.upload_id !== this.boundUploadId)]) {
      if (CLAIMED.has(record.upload_id)) continue;
      let upload: UploadObject;
      try {
        upload = await this.withRetries(
          () => filesRequest<UploadObject>(filesPaths.upload(this.projectId, record.upload_id), { signal }),
          signal,
          null,
        );
      } catch (err) {
        // Offline: keep the record and pause; the next run asks again. A 401
        // is the session: the record outlives signing in again.
        if (isAbortError(err) || (err instanceof FilesRequestError && (err.retryable || err.status === 401))) throw err;
        // Gone, another project's, unreadable: no one can resume it.
        this.forget(record.upload_id);
        continue;
      }
      if (!this.sameFile(upload)) {
        // Another file with this size and time. A dead upload is nobody's; a
        // live or completed one is left for the file it belongs to.
        if (DEAD_UPLOAD.has(upload.status)) this.forget(record.upload_id);
        continue;
      }
      const partsTotal = Math.ceil(this.file.size / record.part_bytes);
      const session: Session = { uploadId: upload.id, partBytes: record.part_bytes, partsTotal, have: new Set(), finished: null };
      if (upload.status === 'pending') {
        this.claim(upload.id);
        this.remember({ ...record, expires_at: upload.expires_at });
        for (const n of this.partsHeld(upload, session)) session.have.add(n);
        this.set({ resumed: true, uploadId: upload.id });
        this.confirmed(session);
        return session;
      }
      if (upload.status === 'completed' && !upload.file) {
        // Completed, and its file deleted since: the resume view reads only a
        // live file. Nothing to finish — and `complete` would replay the old
        // file id, which answers 404 — so this upload is nobody's any more.
        this.forget(record.upload_id);
        continue;
      }
      if (upload.status === 'completed' || upload.status === 'finalizing') {
        // An earlier run's `complete` reached the server and its answer did
        // not reach this tab. The File exists (or is being made): finish from
        // it. `complete` on a completed upload replays its result, and on a
        // finalizing one waits for it, so neither sends a byte again.
        this.claim(upload.id);
        for (let n = 0; n < partsTotal; n += 1) session.have.add(n);
        session.finished = { answer: upload.status === 'completed' ? upload : null };
        this.set({ resumed: true, uploadId: upload.id });
        this.confirmed(session);
        return session;
      }
      // Expired, cancelled, or a status this console does not know: start afresh.
      this.forget(record.upload_id);
    }

    const created = await this.withRetries(
      () =>
        postJson<UploadObject>(
          filesPaths.uploads(this.projectId),
          {
            bytes: this.file.size,
            filename: this.file.name,
            mime_type: this.file.type || 'application/octet-stream',
            purpose: 'user_data',
          },
          signal,
        ),
      signal,
      null,
    );
    const partBytes = partBytesFor(this.deps.partBytes, created.part_max_bytes);
    const partsTotal = Math.ceil(this.file.size / partBytes);
    if (created.max_parts > 0 && partsTotal > created.max_parts) {
      // Refused rather than sent in parts the server would refuse one by one.
      void this.tellServerToCancel(created.id);
      throw new FilesRequestError(400, 'This file is too large to upload from the console. Use the Uploads API.', {
        retryable: false,
      });
    }
    this.claim(created.id);
    this.remember({ upload_id: created.id, part_bytes: partBytes, expires_at: created.expires_at });
    const session: Session = { uploadId: created.id, partBytes, partsTotal, have: new Set(), finished: null };
    this.set({ uploadId: created.id });
    this.confirmed(session);
    return session;
  }

  private async sendMissing(session: Session, signal: AbortSignal): Promise<void> {
    for (let n = 0; n < session.partsTotal; n += 1) {
      if (session.have.has(n)) continue;
      const start = n * session.partBytes;
      const end = Math.min(this.file.size, start + session.partBytes);
      await this.withRetries(
        async () => {
          if (session.have.has(n)) return;
          const part = this.file.slice(start, end);
          const sha = await this.deps.hash(part);
          const headers: Record<string, string> = { 'content-type': 'application/octet-stream' };
          if (sha) headers['x-part-sha256'] = sha;
          await filesRequest<UploadPart>(filesPaths.uploadPart(this.projectId, session.uploadId, n), {
            method: 'PUT',
            headers,
            body: part,
            signal,
          });
        },
        signal,
        session,
      );
      session.have.add(n);
      this.confirmed(session);
    }
  }
}

// ---------------------------------------------------------------------------
// Following a file's processing
// ---------------------------------------------------------------------------

export interface FollowOptions {
  signal: AbortSignal;
  /** Every File object the stream or the poll delivers. */
  onFile: (file: ConsoleFile) => void;
  /** The file no longer exists (deleted, expired). */
  onGone?: () => void;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  /** How often the fallback poll reads the file. */
  pollMs?: number;
}

type StreamOutcome = 'terminal' | 'ended' | 'unavailable' | 'gone' | 'stopped';

const FILE_EVENTS = new Set(['file.processing', 'file.processed', 'file.failed']);

function fileFromFrame(data: string): ConsoleFile | null {
  try {
    const payload = JSON.parse(data) as { type?: unknown; data?: unknown };
    const file = payload?.data as ConsoleFile | undefined;
    return file && typeof file === 'object' && typeof file.id === 'string' ? file : null;
  } catch {
    return null;
  }
}

/**
 * Follow a file until it is processed, failed or gone.
 *
 * The events stream (`GET …/files/{id}/events`, design §2.8) is the source: a
 * `file.processing` frame per change, then exactly one terminal frame. A
 * stream that ends without its terminal frame — a deploy, a proxy that closed
 * an idle connection — is reopened with a growing pause. When the stream is
 * not there at all (an older orchestrator, a BFF without the route) or keeps
 * failing, the follower reads the file every `pollMs` instead: slower, and
 * still true.
 */
export async function followFileEvents(
  projectId: string,
  fileId: string,
  opts: FollowOptions,
): Promise<ConsoleFile | null> {
  const sleep = opts.sleep ?? abortableSleep;
  const pollMs = opts.pollMs ?? 5000;
  const { signal } = opts;
  // A holder, not a `let`: TypeScript does not see assignments made inside
  // the closures below and would narrow a plain variable to null.
  const seen: { file: ConsoleFile | null } = { file: null };
  const deliver = (file: ConsoleFile) => {
    seen.file = file;
    opts.onFile(file);
  };

  /**
   * Read the file once: `yes` delivered it, `gone` is a 404, `stop` is a
   * refusal that polling will not change (signed out, no longer allowed), and
   * `retry` is anything transient.
   */
  const readFile = async (): Promise<'yes' | 'gone' | 'stop' | 'retry'> => {
    try {
      deliver(await filesRequest<ConsoleFile>(filesPaths.file(projectId, fileId), { signal }));
      return 'yes';
    } catch (err) {
      if (isAbortError(err)) throw err;
      if (err instanceof FilesRequestError && err.status === 404) return 'gone';
      if (err instanceof FilesRequestError && !err.retryable) return 'stop';
      return 'retry';
    }
  };

  const streamOnce = async (): Promise<{ outcome: StreamOutcome; frames: number }> => {
    let res: Response;
    try {
      res = await fetch(`${CONSOLE_BASE}${filesPaths.fileEvents(projectId, fileId)}`, {
        headers: { accept: 'text/event-stream' },
        cache: 'no-store',
        signal,
      });
    } catch {
      return { outcome: signal.aborted ? 'stopped' : 'ended', frames: 0 };
    }
    if (res.status === 401) {
      void handleSessionEnd(undefined, fetch, nav);
      return { outcome: 'stopped', frames: 0 };
    }
    const isStream = (res.headers.get('content-type') ?? '').includes('text/event-stream');
    if (!res.ok || !isStream || !res.body) {
      await res.body?.cancel().catch(() => undefined);
      if (res.status === 404) {
        // The BFF's "unknown endpoint" and the route's file_not_found are both
        // 404s; the file's own read tells them apart.
        const read = await readFile();
        return { outcome: read === 'gone' ? 'gone' : read === 'stop' ? 'stopped' : 'unavailable', frames: 0 };
      }
      if (res.ok || res.status === 400 || res.status === 405 || res.status === 501) {
        return { outcome: 'unavailable', frames: 0 };
      }
      return { outcome: 'ended', frames: 0 };
    }
    const parser = new SSEParser();
    const decoder = new TextDecoder();
    const reader = res.body.getReader();
    let frames = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const event of parser.feed(decoder.decode(value, { stream: true }))) {
          if (!FILE_EVENTS.has(event.event)) continue;
          const file = fileFromFrame(event.data);
          if (!file) continue;
          frames += 1;
          deliver(file);
          if (event.event !== 'file.processing' || isTerminal(file)) {
            await reader.cancel().catch(() => undefined);
            return { outcome: file.status === 'deleted' ? 'gone' : 'terminal', frames };
          }
        }
      }
    } catch {
      return { outcome: signal.aborted ? 'stopped' : 'ended', frames };
    }
    return { outcome: 'ended', frames };
  };

  let failures = 0;
  let polling = false;
  try {
    while (!signal.aborted && !polling) {
      const { outcome, frames } = await streamOnce();
      if (outcome === 'terminal' || outcome === 'stopped') return seen.file;
      if (outcome === 'gone') {
        opts.onGone?.();
        return seen.file;
      }
      if (outcome === 'unavailable') {
        polling = true;
        break;
      }
      failures = frames > 0 ? 1 : failures + 1;
      if (failures >= 4) {
        polling = true;
        break;
      }
      await sleep(Math.min(15_000, 1000 * 2 ** (failures - 1)), signal);
    }
    while (!signal.aborted) {
      const read = await readFile();
      if (read === 'gone') {
        opts.onGone?.();
        return seen.file;
      }
      if (read === 'stop') return seen.file;
      if (seen.file && isTerminal(seen.file)) return seen.file;
      await sleep(pollMs, signal);
    }
  } catch (err) {
    if (!isAbortError(err)) throw err;
  }
  return seen.file;
}
