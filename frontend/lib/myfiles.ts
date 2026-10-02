/**
 * "My files" — everything a person uploaded, across every chat (2026-09-30).
 *
 * WHY THIS EXISTS. The owner asked why an upload "is stored, right, but it
 * does not show on the user side". Until this page the only upload a person
 * could list was a voice recording (/recordings); documents, spreadsheets,
 * videos and audio files could be reached only from the chat they were sent
 * in, and only while that chat was open.
 *
 * THE ROUTES (orchestrator/app/myfiles.py):
 *   GET /api/files/mine?q=&kind=&since=&until=&min_bytes=&max_bytes=&sort=&limit=&cursor=
 *       -> {items: [...], next_cursor, retention}
 *   GET /api/files/mine/summary?q=&since=&until=&min_bytes=&max_bytes=
 *       -> {kinds: {kind: {count, bytes}}, total, retention}
 * Both are read-only and scoped to the session. Nothing here fetches bytes:
 * Download points at the STREAMING proxies that already exist
 * (/api/uploads/{conv}/{upload}/file and /api/audio/sessions/{id}/audio), and
 * a preview reuses the loaders the chat's attachment cards use.
 *
 * WHAT A ROW SAYS IS WHAT IS STORED. `availability` is the server's answer,
 * decided the way the download route decides it: the original bytes
 * (available), only the text the chat read (text_only), only a spreadsheet's
 * summary (summary_only), a recording still being made (processing), or
 * nothing (expired). The page never offers an action the row cannot back.
 *
 * PICTURES (2026-10-02, docs/chat-media/CONTRACT.md §9). A photo sent in a
 * chat is now kept on the server for the life of the chat (the `chat_media`
 * table), so it is listed here too: kind `image`, shown by its thumbnail,
 * previewed at full size. Its URLs are BUILT here from the chat and the
 * photo's attachment id (lib/chatMedia), never taken from the row, so an
 * `<img src>` on this page only ever points at this app's own photo route.
 */

import { previewKindFor, uploadFileUrl, type UploadRef } from './attachments';
import { ATTACHMENT_ID, CONVERSATION_ID, chatMediaUrl, type MediaRef } from './chatMedia';
import {
  MAX_TABLE_COLUMNS,
  MAX_TABLE_ROWS,
  workbookFromProfile,
  type SheetProfile,
  type WorkbookPreview,
} from './previewData';
import { recordingAudioUrl } from './recordings';

export const MY_FILES_URL = '/api/files/mine';
export const MY_FILES_SUMMARY_URL = '/api/files/mine/summary';

/** Rows per request: the server's default, and a page that still scrolls on a phone. */
export const MY_FILES_PAGE_SIZE = 50;

/** The server refuses a longer search (myfiles.QUERY_MAX_CHARS). */
export const SEARCH_MAX_CHARS = 100;

/**
 * The largest file a preview downloads to draw. A preview is one screen; a
 * 200 MB PDF pulled into the tab to show its first page is not worth the
 * phone's data, and Download is right next to it.
 */
export const BYTE_PREVIEW_MAX_BYTES = 25 * 1024 * 1024;

export const FILE_KINDS = ['document', 'dataset', 'image', 'video', 'audio', 'recording'] as const;
export type FileKind = (typeof FILE_KINDS)[number];
/**
 * `media` is a stored chat picture (a `chat_media` row). The backend files
 * track was building those rows alongside this page, so the two spellings it
 * might reasonably have chosen for the source are read as the same thing
 * (docs/chat-media/NOTES.md, fe-files).
 */
export type FileSource = 'upload' | 'text' | 'recording' | 'media';
const MEDIA_SOURCES = ['media', 'image', 'chat_media'];
export const AVAILABILITIES = ['available', 'text_only', 'summary_only', 'processing', 'expired'] as const;
export type Availability = (typeof AVAILABILITIES)[number];
export type ServerPreview = 'text' | 'summary' | 'audio' | 'image' | null;
export const SORTS = ['newest', 'oldest', 'largest', 'name'] as const;
export type FileSort = (typeof SORTS)[number];
export const SIZE_BUCKETS = ['under_1mb', '1_10mb', '10_100mb', 'over_100mb'] as const;
export type SizeBucket = (typeof SIZE_BUCKETS)[number];

export interface MyFile {
  /** `upload:<id>`, `text:<id>` or `recording:<id>` — unique across sources. */
  id: string;
  source: FileSource;
  kind: FileKind;
  name: string;
  /** null for a document whose text is all that was ever kept. */
  bytes: number | null;
  createdAt: string;
  /** The chat it was sent in; null for a voice recording. */
  conversation: { id: string; title: string } | null;
  availability: Availability;
  /**
   * `hasTranscript`: a recording whose transcript has words, which the
   * Recordings page shows. The server decides; false for everything else.
   */
  media: { status: string | null; durationMs: number | null; hasTranscript: boolean } | null;
  can: { download: boolean; preview: ServerPreview; delete: boolean };
  /**
   * The stored text a `text` preview reads (GET /uploads/{conv}/document
   * ?name=), when the server named one: an archive sent as a document keeps
   * "<name> (archive contents)", never a text row under its own name.
   */
  textName: string | null;
  /** Derived from `id`: the upload's own id, for the file routes. */
  uploadId: string | null;
  /** Derived from `id`: the recording session's id. */
  recordingId: string | null;
  /**
   * A stored chat picture's reference: its chat and attachment id, which is
   * what every picture URL is built from. null for every other kind.
   */
  picture: MediaRef | null;
}

export interface Retention {
  uploadHours: number;
  /**
   * 2026-10-02 (CONTRACT §9): documents and datasets get a lasting copy that
   * stays while their chat exists, so `uploadHours` then describes only
   * files sent before that (or skipped when the server was short of space).
   * Read from `files_kept_with_chat`; false from an orchestrator without it.
   */
  filesKeptWithChat: boolean;
  /** 0 keeps recordings until their owner deletes them. */
  recordingDays: number;
  videoKeptWithChat: boolean;
  videoGraceHours: number | null;
  /** An orchestrator from before pictures were stored (`"browser_only"`). */
  picturesBrowserOnly: boolean;
  /** Pictures are kept with their chat and listed here (any other value). */
  picturesKept: boolean;
  pictureMemoryHours: number | null;
}

export interface MyFilesPage {
  items: MyFile[];
  /** Hand back as `cursor` for the next (older, by the current sort) page. */
  nextCursor: string | null;
  retention: Retention | null;
}

export interface KindCount {
  count: number;
  bytes: number;
}

export interface MyFilesSummary {
  kinds: Record<FileKind, KindCount>;
  total: KindCount;
  retention: Retention | null;
  /**
   * The kinds the server actually counted. A missing one still reads as zero
   * in `kinds`, but the page does not offer to FILTER by a kind the server
   * never named: an orchestrator without picture rows refuses `kind=image`.
   */
  reported: FileKind[];
}

/** What the page URL holds. `from`/`to` are calendar days, YYYY-MM-DD. */
export interface Filters {
  q: string;
  kind: FileKind | null;
  from: string;
  to: string;
  size: SizeBucket | null;
  sort: FileSort;
}

export const DEFAULT_FILTERS: Filters = { q: '', kind: null, from: '', to: '', size: null, sort: 'newest' };

/* ------------------------------------------------------------- words */

export const KIND_LABEL: Record<FileKind, string> = {
  document: 'Document',
  dataset: 'Spreadsheet or data',
  image: 'Picture',
  video: 'Video',
  audio: 'Audio',
  recording: 'Voice recording',
};

export const KIND_FILTER_LABEL: Record<FileKind, string> = {
  document: 'Documents',
  dataset: 'Spreadsheets & data',
  image: 'Pictures',
  video: 'Videos',
  audio: 'Audio',
  recording: 'Voice recordings',
};

export const AVAILABILITY_LABEL: Record<Availability, string> = {
  available: 'Stored',
  text_only: 'Text only',
  summary_only: 'Summary only',
  processing: 'Processing',
  expired: 'Removed',
};

export const SIZE_LABEL: Record<SizeBucket, string> = {
  under_1mb: 'Under 1 MB',
  '1_10mb': '1 to 10 MB',
  '10_100mb': '10 to 100 MB',
  over_100mb: 'Over 100 MB',
};

export const SORT_LABEL: Record<FileSort, string> = {
  newest: 'Newest first',
  oldest: 'Oldest first',
  largest: 'Largest first',
  name: 'Name, A to Z',
};

const MB = 1024 * 1024;
/** Half-open byte ranges, [min, max): the buckets partition every size. */
const SIZE_RANGE: Record<SizeBucket, { min?: number; max?: number }> = {
  under_1mb: { max: MB },
  '1_10mb': { min: MB, max: 10 * MB },
  '10_100mb': { min: 10 * MB, max: 100 * MB },
  over_100mb: { min: 100 * MB },
};

function hours(n: number): string {
  return `${n} hour${n === 1 ? '' : 's'}`;
}

/**
 * How long each kind is kept, from the deployment's own settings (the
 * server sends them with every page), so the page stays true when a TTL
 * changes.
 *
 * Each sentence says what the server does, no more and no less. A chat
 * file's stored copy is kept for WORKSPACE_TTL_HOURS: the sweep never removes
 * an upload younger than that, not even to stay under WORKSPACE_QUOTA_GB
 * (core/repo._upload_is_live). After that it goes the next time the sweep
 * runs, which is only when someone uploads a file or a repository is cloned,
 * so on a quiet server a file stays stored, and listed as Stored, past its
 * hours. "Up to" stated a maximum nothing enforces (QA 2026-10-01). Deleting
 * a chat drops its rows at once but leaves the bytes to the next sweep or the
 * video reaper, so no erasure time is given.
 */
export function retentionSentences(r: Retention): string[] {
  const out = [
    `Files you attach to a chat are kept for ${hours(r.uploadHours)}, then removed the next time the server clears out old files; after that the chat keeps what it read (a document's text, a spreadsheet's summary).`,
  ];
  if (r.filesKeptWithChat) {
    // The lasting copy (CONTRACT §9) gets its own sentence: the one above
    // still holds for the workspace copy and for a file sent before copies
    // were kept, or while the server was short of space.
    out.push(
      'Documents and spreadsheets also keep a copy that stays while their chat exists, unless the server was short of space when they were sent.',
    );
  }
  if (r.videoKeptWithChat) out.push('Videos and audio files stay while their chat exists.');
  if (r.picturesKept) {
    // The backfill (lib/chatMedia): a photo sent before pictures were stored
    // reaches the server only from the browser that still holds it.
    out.push(
      'Pictures stay while their chat exists. One sent before pictures were kept appears here once the browser that sent it opens its chat again.',
    );
  }
  out.push('Deleting a chat takes its files off this list at once; the server erases their stored copies later.');
  out.push(
    r.recordingDays > 0
      ? `Voice recordings are deleted automatically ${r.recordingDays} day${r.recordingDays === 1 ? '' : 's'} after they finish.`
      : 'Voice recordings stay until you delete them.',
  );
  if (r.picturesBrowserOnly) out.push('Pictures stay only in the browser you sent them from.');
  return out;
}

const ARCHIVE_NAME = /\.(zip|tar|tgz|tar\.gz)$/i;

/**
 * The sentence under a row that is not simply "stored": what happened to the
 * file, and what of it is left. null for a stored file.
 */
export function availabilityNote(file: MyFile, retention: Retention | null): string | null {
  // True as worded: nothing removes a chat file before its hours are up.
  const after = retention ? ` after ${hours(retention.uploadHours)}` : '';
  switch (file.availability) {
    case 'text_only':
      return file.source === 'text'
        ? 'Only the text the chat read was kept; the file itself was not stored.'
        : `The file was removed${after}. The text the chat read is kept.`;
    case 'summary_only':
      return ARCHIVE_NAME.test(file.name)
        ? 'An archive is unpacked when it arrives, so the archive itself is not kept. The summary the chat made is.'
        : `The file was removed${after}. The summary the chat made of it is kept.`;
    case 'processing':
      return 'Still being recorded or transcribed.';
    case 'expired':
      if (file.kind === 'image') return 'This picture is no longer stored.';
      return file.kind === 'video' || file.kind === 'audio'
        ? 'This file is no longer stored.'
        : `The file was removed${after}, and nothing of it was kept.`;
    default:
      return null;
  }
}

/* --------------------------------------------------------- the URL */

const DAY = /^(\d{4})-(\d{2})-(\d{2})$/;

function validDay(value: string | null): string {
  if (!value) return '';
  const m = DAY.exec(value);
  if (!m) return '';
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const date = new Date(y, mo - 1, d);
  return date.getFullYear() === y && date.getMonth() === mo - 1 && date.getDate() === d ? value : '';
}

/** Local midnight at the start of `day`, as the instant the server compares. */
export function dayStartIso(day: string): string | null {
  const ok = validDay(day);
  if (!ok) return null;
  const [y, m, d] = ok.split('-').map(Number) as [number, number, number];
  return new Date(y, m - 1, d).toISOString();
}

/** Local midnight at the start of the NEXT day: `to` includes the day it names. */
export function nextDayStartIso(day: string): string | null {
  const ok = validDay(day);
  if (!ok) return null;
  const [y, m, d] = ok.split('-').map(Number) as [number, number, number];
  return new Date(y, m - 1, d + 1).toISOString();
}

function oneOf<T extends string>(values: readonly T[], raw: string | null): T | null {
  return raw !== null && (values as readonly string[]).includes(raw) ? (raw as T) : null;
}

/** The page URL's query, read defensively: anything unknown is the default. */
export function filtersFromQuery(params: { get(name: string): string | null }): Filters {
  return {
    q: (params.get('q') ?? '').slice(0, SEARCH_MAX_CHARS),
    kind: oneOf(FILE_KINDS, params.get('kind')),
    from: validDay(params.get('from')),
    to: validDay(params.get('to')),
    size: oneOf(SIZE_BUCKETS, params.get('size')),
    sort: oneOf(SORTS, params.get('sort')) ?? 'newest',
  };
}

/** The page URL's query for `filters`, defaults left out ('' for none). */
export function filtersToQuery(filters: Filters): string {
  const q = new URLSearchParams();
  if (filters.q) q.set('q', filters.q);
  if (filters.kind) q.set('kind', filters.kind);
  if (filters.from) q.set('from', filters.from);
  if (filters.to) q.set('to', filters.to);
  if (filters.size) q.set('size', filters.size);
  if (filters.sort !== 'newest') q.set('sort', filters.sort);
  return q.toString();
}

/** Anything narrowing the list (the sort only reorders it). */
export function hasActiveFilters(filters: Filters): boolean {
  return Boolean(filters.q || filters.kind || filters.from || filters.to || filters.size);
}

function sharedParams(filters: Filters): URLSearchParams {
  const q = new URLSearchParams();
  const search = filters.q.trim();
  if (search) q.set('q', search);
  const since = dayStartIso(filters.from);
  if (since) q.set('since', since);
  const until = nextDayStartIso(filters.to);
  if (until) q.set('until', until);
  if (filters.size) {
    const range = SIZE_RANGE[filters.size];
    if (range.min !== undefined) q.set('min_bytes', String(range.min));
    if (range.max !== undefined) q.set('max_bytes', String(range.max));
  }
  return q;
}

/** GET /api/files/mine for these filters and this page. */
export function listRequestUrl(filters: Filters, cursor: string | null, limit = MY_FILES_PAGE_SIZE): string {
  const q = sharedParams(filters);
  if (filters.kind) q.set('kind', filters.kind);
  if (filters.sort !== 'newest') q.set('sort', filters.sort);
  q.set('limit', String(limit));
  if (cursor) q.set('cursor', cursor);
  return `${MY_FILES_URL}?${q.toString()}`;
}

/** GET /api/files/mine/summary: the same filters, but counts are per kind. */
export function summaryRequestUrl(filters: Filters): string {
  const query = sharedParams(filters).toString();
  return query ? `${MY_FILES_SUMMARY_URL}?${query}` : MY_FILES_SUMMARY_URL;
}

/** The chat a file was sent in (ChatApp opens ?c= on load). */
export function chatUrl(conversationId: string): string {
  return `/?c=${encodeURIComponent(conversationId)}`;
}

/* ----------------------------------------------------------- parsing */

const HEX32 = /^[0-9a-f]{32}$/;
const ID_SHAPE = /^(upload|text|recording|media|image|chat_media):([A-Za-z0-9_-]{1,64})$/;
/** A picture URL the server may send instead of the bare attachment id. */
const PICTURE_URL = /\/chat-media\/([A-Za-z0-9_-]{1,64})\/([A-Za-z0-9_-]{8,64})(?:[?#]|$)/;
const EXTENSION_BY_MIME: Record<string, string> = {
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/webp': 'webp',
  'image/gif': 'gif',
};

const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const record = (v: unknown): Record<string, unknown> | null =>
  typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null;

function parseRetention(raw: unknown): Retention | null {
  const r = record(raw);
  if (!r) return null;
  const uploadHours = num(r.upload_hours);
  const recordingDays = num(r.recording_days);
  if (uploadHours === null || recordingDays === null) return null;
  const pictures = str(r.pictures);
  return {
    uploadHours,
    filesKeptWithChat: r.files_kept_with_chat === true,
    recordingDays,
    videoKeptWithChat: r.video_kept_with_chat === true,
    videoGraceHours: num(r.video_grace_hours),
    picturesBrowserOnly: pictures === 'browser_only',
    // Any other word means the server keeps them; only an explicit
    // "browser_only" (or no word at all) keeps the old sentence.
    picturesKept: Boolean(pictures) && pictures !== 'browser_only',
    pictureMemoryHours: num(r.picture_memory_hours),
  };
}

/**
 * A stored picture's reference: the attachment id the row names (or, from a
 * server that sent a URL instead, the one in that URL — and only when it is
 * a URL for this same chat). null when there is none to be had, which drops
 * the row: without it the page can neither show nor open the picture.
 */
function pictureRef(r: Record<string, unknown>, conversationId: string): MediaRef | null {
  if (!CONVERSATION_ID.test(conversationId)) return null;
  const media = record(r.media);
  const named = [r.attachment_id, media?.attachment_id].find(
    (v): v is string => typeof v === 'string' && ATTACHMENT_ID.test(v),
  );
  if (named) return { conversationId, attachmentId: named };
  for (const url of [r.thumb_url, r.thumbnail_url]) {
    const m = typeof url === 'string' ? PICTURE_URL.exec(url) : null;
    if (m && m[1] === conversationId) return { conversationId, attachmentId: m[2]! };
  }
  return null;
}

/** A picture's name, which the server may not have (chat_media keeps none). */
function pictureName(r: Record<string, unknown>): string {
  const named = str(r.name)?.trim();
  if (named) return named;
  const mime = (str(record(r.media)?.mime) ?? str(r.mime) ?? '').toLowerCase();
  const ext = EXTENSION_BY_MIME[mime];
  return ext ? `Picture.${ext}` : 'Picture';
}

function parseFile(raw: unknown): MyFile | null {
  const r = record(raw);
  if (!r) return null;
  const id = str(r.id);
  const shape = id ? ID_SHAPE.exec(id) : null;
  const source = str(r.source);
  const kind = oneOf(FILE_KINDS, str(r.kind));
  const availability = oneOf(AVAILABILITIES, str(r.availability));
  const createdAt = str(r.created_at);
  const can = record(r.can);
  if (!id || !shape || shape[1] !== source || !kind || !availability || !createdAt || !can) return null;
  if (Number.isNaN(Date.parse(createdAt))) return null;
  // A picture comes from the picture store and nothing else does: a mismatch
  // either way is a row this page would act on wrongly.
  const isPicture = MEDIA_SOURCES.includes(source);
  if (isPicture !== (kind === 'image')) return null;
  const name = isPicture ? pictureName(r) : str(r.name);
  if (!name) return null;
  // A recording is named by a server id the audio routes accept, and nothing
  // else; a chat file cannot be opened, previewed or downloaded without its chat.
  if (source === 'recording' && (kind !== 'recording' || !HEX32.test(shape[2]!))) return null;
  const conv = record(r.conversation);
  const conversation =
    conv && typeof conv.id === 'string' && conv.id ? { id: conv.id, title: str(conv.title) ?? '' } : null;
  if (source !== 'recording' && !conversation) return null;
  const picture = isPicture && conversation ? pictureRef(r, conversation.id) : null;
  if (isPicture && !picture) return null;
  const media = isPicture ? null : record(r.media);
  const preview = oneOf(['text', 'summary', 'audio', 'image'] as const, str(can.preview));
  return {
    id,
    source: isPicture ? 'media' : (source as FileSource),
    kind,
    name,
    bytes: num(r.bytes),
    createdAt,
    conversation,
    availability,
    media: media
      ? {
          status: str(media.status),
          durationMs: num(media.duration_ms),
          hasTranscript: media.has_transcript === true,
        }
      : null,
    // Only an explicit `true` grants an action.
    can: { download: can.download === true, preview, delete: can.delete === true },
    textName: preview === 'text' ? str(r.text_name) || null : null,
    uploadId: source === 'upload' ? shape[2]! : null,
    recordingId: source === 'recording' ? shape[2]! : null,
    picture,
  };
}

/** One list page, dropping any row this page could not act on safely. */
export function parseMyFilesPage(body: unknown): MyFilesPage | null {
  const b = record(body);
  if (!b || !Array.isArray(b.items)) return null;
  const items = b.items.flatMap((raw) => {
    const file = parseFile(raw);
    return file ? [file] : [];
  });
  return { items, nextCursor: str(b.next_cursor), retention: parseRetention(b.retention) };
}

function parseCount(raw: unknown): KindCount {
  const r = record(raw);
  return { count: num(r?.count) ?? 0, bytes: num(r?.bytes) ?? 0 };
}

export function parseSummary(body: unknown): MyFilesSummary | null {
  const b = record(body);
  const kinds = record(b?.kinds);
  if (!b || !kinds) return null;
  const parsed = Object.fromEntries(FILE_KINDS.map((k) => [k, parseCount(kinds[k])])) as Record<FileKind, KindCount>;
  return {
    kinds: parsed,
    total: parseCount(b.total),
    retention: parseRetention(b.retention),
    reported: FILE_KINDS.filter((k) => record(kinds[k]) !== null),
  };
}

/** Append an older page without repeating a row the list already shows. */
export function appendFiles(current: MyFile[], older: MyFile[]): MyFile[] {
  const seen = new Set(current.map((f) => f.id));
  return [...current, ...older.filter((f) => !seen.has(f.id))];
}

/** A DOM id for the row's heading: letters, digits and dashes only. */
export function rowDomId(file: MyFile): string {
  return `file-${file.id.replace(':', '-').replace(/[^A-Za-z0-9_-]/g, '')}`;
}

/* ---------------------------------------------------------- actions */

function uploadRef(file: MyFile): UploadRef | null {
  if (file.source !== 'upload' || !file.uploadId || !file.conversation || !HEX32.test(file.uploadId)) return null;
  return { conversationId: file.conversation.id, uploadId: file.uploadId };
}

/** Where Download points, or null when the server did not offer one. */
export function downloadUrl(file: MyFile): string | null {
  if (!file.can.download) return null;
  if (file.source === 'recording' && file.recordingId) return recordingAudioUrl(file.recordingId);
  if (file.picture) return chatMediaUrl(file.picture, 'full');
  const ref = uploadRef(file);
  return ref ? uploadFileUrl(ref) : null;
}

export type PreviewPlan =
  /** The stored bytes, drawn by the browser: a PDF, a picture, text or CSV. */
  | { kind: 'bytes'; ref: UploadRef }
  /** The text the chat extracted (engines/document.py). */
  | { kind: 'text'; conversationId: string; name: string }
  /** The profile stored when a spreadsheet or dataset arrived. */
  | { kind: 'summary'; conversationId: string; uploadId: string }
  /** A recording's own player. */
  | { kind: 'audio'; url: string }
  /** A stored chat picture, opened at full size from the picture route. */
  | { kind: 'picture'; ref: MediaRef };

/**
 * The preview a row gets, best first. Bytes only for the formats the
 * attachment preview draws itself (its allowlist: never HTML, SVG or script,
 * whatever the file is named) and only up to BYTE_PREVIEW_MAX_BYTES; then
 * what the server kept about the file; else none.
 */
export function previewPlanFor(file: MyFile): PreviewPlan | null {
  if (file.source === 'recording') {
    return file.can.preview === 'audio' && file.recordingId
      ? { kind: 'audio', url: recordingAudioUrl(file.recordingId) }
      : null;
  }
  if (file.picture) {
    // Only verified rasters are stored (CONTRACT §3), at most 10 MiB each, so
    // a stored picture always previews while the server says it is there.
    return file.availability === 'available' ? { kind: 'picture', ref: file.picture } : null;
  }
  const ref = uploadRef(file);
  const drawable = ['image', 'pdf', 'text'].includes(previewKindFor(file.name));
  if (
    ref &&
    file.availability === 'available' &&
    drawable &&
    file.bytes !== null &&
    file.bytes <= BYTE_PREVIEW_MAX_BYTES
  ) {
    return { kind: 'bytes', ref };
  }
  if (file.can.preview === 'text' && file.conversation) {
    return { kind: 'text', conversationId: file.conversation.id, name: file.textName ?? file.name };
  }
  if (file.can.preview === 'summary' && ref) {
    return { kind: 'summary', conversationId: ref.conversationId, uploadId: ref.uploadId };
  }
  return null;
}

/* ---------------------------------------------------------- summary */

const asRecord = (v: unknown): Record<string, unknown> => record(v) ?? {};

function tableSheet(entry: Record<string, unknown>): SheetProfile {
  const columns = (Array.isArray(entry.columns) ? entry.columns : [])
    .map((c) => {
      const rec = asRecord(c);
      return typeof rec.name === 'string' ? rec.name : String(c ?? '');
    })
    .slice(0, MAX_TABLE_COLUMNS);
  // `full_rows` is the whole table (small files); `sample_rows` a handful. The
  // flag is carried so the dialog never calls a sample the complete table.
  const full = Array.isArray(entry.full_rows) ? entry.full_rows : null;
  const source = full ?? (Array.isArray(entry.sample_rows) ? entry.sample_rows : []);
  return {
    name: typeof entry.file === 'string' ? entry.file : 'Table',
    rows: typeof entry.rows === 'number' ? entry.rows : source.length,
    columns,
    previewRows: source.slice(0, MAX_TABLE_ROWS).map(asRecord),
    complete: Boolean(full),
  };
}

/** Tabs a summary shows at most: an archive can hold hundreds of tables. */
const MAX_SUMMARY_TABLES = 10;

/**
 * A spreadsheet's or dataset's stored profile as something the preview
 * dialog draws: a workbook's own sheets, or each profiled table (a CSV, or
 * the tables inside an archive) as one sheet named after its file.
 */
export function summaryFromProfile(profile: unknown, filename: string): WorkbookPreview | null {
  const workbook = workbookFromProfile(profile, filename);
  if (workbook) return workbook;
  const entries = (Array.isArray(profile) ? profile : []).map(asRecord).filter((e) => e.kind === 'table');
  const sheets = entries
    .slice(0, MAX_SUMMARY_TABLES)
    .map(tableSheet)
    .filter((s) => s.columns.length > 0 || s.previewRows.length > 0);
  return sheets.length ? { filename, sheets } : null;
}

/* ---------------------------------------------------------- requests */

export type LoadResult<T> =
  | { kind: 'ok'; value: T }
  | { kind: 'failed'; message: string; signedOut: boolean }
  | { kind: 'aborted' };

const UNREACHABLE = "The server couldn't be reached. Check your connection and try again.";

function isAbort(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { name?: unknown }).name === 'AbortError';
}

/** The sentence a refusal carries: flat {detail}, a proxy's {message}, or nested FastAPI. */
function detailOf(body: unknown): string | null {
  const b = record(body);
  if (!b) return null;
  if (typeof b.detail === 'string') return b.detail;
  const nested = record(b.detail);
  if (nested && typeof nested.detail === 'string') return nested.detail;
  return typeof b.message === 'string' ? b.message : null;
}

async function getJson<T>(
  fetchFn: typeof fetch,
  url: string,
  parse: (body: unknown) => T | null,
  signal?: AbortSignal,
): Promise<LoadResult<T>> {
  const lead = "Your files couldn't be loaded.";
  let res: Response;
  try {
    res = await fetchFn(url, { method: 'GET', cache: 'no-store', signal });
  } catch (err) {
    if (signal?.aborted || isAbort(err)) return { kind: 'aborted' };
    return { kind: 'failed', message: `${lead} ${UNREACHABLE}`, signedOut: false };
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch (err) {
    if (signal?.aborted || isAbort(err)) return { kind: 'aborted' };
    body = null;
  }
  if (signal?.aborted) return { kind: 'aborted' };
  if (res.ok) {
    const value = parse(body);
    return value
      ? { kind: 'ok', value }
      : { kind: 'failed', message: `${lead} The server's answer couldn't be read. Try again.`, signedOut: false };
  }
  if (res.status === 401) {
    return { kind: 'failed', message: `${lead} You were signed out. Sign in and try again.`, signedOut: true };
  }
  if (res.status === 404) {
    return { kind: 'failed', message: `${lead} This server doesn't have My files yet.`, signedOut: false };
  }
  const detail = detailOf(body);
  if (res.status === 400 && detail) {
    return { kind: 'failed', message: `${lead} ${detail}`, signedOut: false };
  }
  if (res.status >= 502 && res.status <= 504) {
    return { kind: 'failed', message: `${lead} ${UNREACHABLE}`, signedOut: false };
  }
  return {
    kind: 'failed',
    message: `${lead} The server answered with error ${res.status}. Try again.`,
    signedOut: false,
  };
}

export function loadFiles(
  fetchFn: typeof fetch,
  filters: Filters,
  cursor: string | null,
  signal?: AbortSignal,
): Promise<LoadResult<MyFilesPage>> {
  return getJson(fetchFn, listRequestUrl(filters, cursor), parseMyFilesPage, signal);
}

export function loadSummary(
  fetchFn: typeof fetch,
  filters: Filters,
  signal?: AbortSignal,
): Promise<LoadResult<MyFilesSummary>> {
  return getJson(fetchFn, summaryRequestUrl(filters), parseSummary, signal);
}
