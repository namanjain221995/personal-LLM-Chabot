/**
 * In-memory store of the attachment that was sent with a user turn.
 *
 * Regenerate/retry re-send an earlier turn, but the message we persist keeps
 * only a preview (`imageDataUrl`) or a filename (`pdfName`) — never the raw
 * payload. Re-sending without it silently changed the question: the model was
 * asked to re-answer "what's in this invoice?" with no invoice attached.
 *
 * The payload deliberately does NOT go into localStorage. A 25 MB PDF as
 * base64 would blow the quota, and quota eviction is exactly what caused the
 * conversation-destroying bug this codebase already had to fix. Holding it in
 * memory means a regenerate works for the lifetime of the tab, and after a
 * reload the user is told to re-attach rather than being handed a silently
 * different answer.
 */

export interface SentAttachment {
  kind: 'image' | 'pdf';
  name: string;
  /** Raw base64, no data: prefix — what POST /chat expects. */
  base64: string;
}

const sent = new Map<string, SentAttachment[]>();

/** Remember what was attached to the user message with this id —
    up to 5 images or a single PDF (2026-08-05 multi-upload). */
export function rememberAttachments(
  messageId: string,
  attachments: SentAttachment[],
): void {
  if (attachments.length) sent.set(messageId, attachments);
}

/** Strip a `data:...;base64,` prefix, returning the raw payload. */
export function base64FromDataUrl(dataUrl?: string | null): string | null {
  if (!dataUrl) return null;
  const comma = dataUrl.indexOf(',');
  if (!dataUrl.startsWith('data:') || comma === -1) return null;
  const payload = dataUrl.slice(comma + 1);
  return payload || null;
}

export interface AttachmentsLookup {
  /** The attachments to re-send, when we still have them (may be empty). */
  attachments: SentAttachment[];
  /** True when the turn HAD attachments we can no longer reconstruct. */
  missing: boolean;
}

/**
 * Recover the attachments for a user turn being re-sent.
 *
 * Images survive a reload: the persisted `imageDataUrls` (or the legacy
 * single `imageDataUrl`) ARE the payloads. PDFs do not — only the filename
 * is kept — so after a reload they report `missing`, and the caller must ask
 * the user to re-attach instead of quietly sending a text-only prompt.
 */
export function attachmentsForResend(message: {
  id: string;
  imageDataUrl?: string;
  imageDataUrls?: string[];
  pdfName?: string;
}): AttachmentsLookup {
  const remembered = sent.get(message.id);
  if (remembered) return { attachments: remembered, missing: false };

  const previews = message.imageDataUrls?.length
    ? message.imageDataUrls
    : message.imageDataUrl
      ? [message.imageDataUrl]
      : [];
  const fromPreviews = previews
    .map((p) => base64FromDataUrl(p))
    .filter((b): b is string => Boolean(b))
    .map((base64) => ({ kind: 'image' as const, name: 'image', base64 }));
  if (fromPreviews.length === previews.length && fromPreviews.length > 0) {
    return { attachments: fromPreviews, missing: false };
  }
  return {
    attachments: [],
    missing: Boolean(
      message.pdfName || message.imageDataUrl || message.imageDataUrls?.length,
    ),
  };
}

/* ==========================================================================
   NEW-09 / NEW-09A — PREVIEWING the attachment that is already on screen.

   The store above keeps a base64 payload so a REGENERATE can re-ask the same
   question. It is the wrong shape for showing a file: it is text, it exists
   only for the two kinds the chat request carries, and it is keyed by nothing
   but the message.

   What follows is the viewing layer. It holds the original browser `File` for
   the lifetime of the tab, keyed by (messageId, index) — never by filename,
   because one conversation is perfectly entitled to contain two attachments
   called `invoice.pdf`. Nothing here is persisted, nothing is sent anywhere,
   and it is independent of the dataset upload path: a dataset still streams to
   /api/upload exactly as before, and this only remembers the same handle.

   NEW-09A — WHAT THIS MODULE DELIBERATELY NO LONGER DOES.

   The first version of this file downloaded things. It had an `<a download>`
   for every format the browser could not render, it typed those blobs
   `application/octet-stream` to make sure they saved, and when a popup blocker
   ate the preview tab it fell back to downloading as well. Manual testing found
   all three: clicking a .docx put a file in the Downloads tray, and even the
   "preview" path did, because `window.open` on a blob: URL is a download
   instruction to Chrome whenever it cannot render the type inline.

   So this module now mints URLs and classifies formats, and NOTHING here
   navigates, opens a tab, or saves a file. The only consumer is
   components/AttachmentPreview, an in-app dialog — because a dialog is
   steerable and the browser's blob navigation is not.
   ========================================================================== */

export interface AttachmentBlob {
  name: string;
  /** The browser's own MIME for the file. Advisory only — see previewKindFor. */
  mime: string;
  blob: Blob;
}

/** Positional: index N is the Nth attachment shown on that message. A hole
    (null) means "this one was never readable", not "shift everything up". */
const held = new Map<string, Array<AttachmentBlob | null>>();

/**
 * Keep the raw files of the attachments sent with `messageId`, in the order
 * they are rendered in.
 */
export function rememberAttachmentFiles(
  messageId: string,
  files: Array<AttachmentBlob | null>,
): void {
  if (files.some(Boolean)) held.set(messageId, files);
}

/** The file behind the Nth attachment of a message, while this tab lives. */
export function attachmentFile(
  messageId: string,
  index: number,
): AttachmentBlob | null {
  return held.get(messageId)?.[index] ?? null;
}

/**
 * Editing a turn appends a NEW message carrying the same attachments, so the
 * files have to follow it or the rewritten turn's card would go dead.
 */
export function carryAttachmentFiles(fromId: string, toId: string): void {
  const files = held.get(fromId);
  if (files) held.set(toId, files);
}

/* ----------------------------------------------------- what can be shown */

/**
 * How the preview dialog should render this file.
 *
 * An allowlist by EXTENSION, deliberately: the MIME a browser reports for a
 * dropped file is derived from that file's own name, so it is attacker-chosen
 * and may not decide what we render. `shot.png` previews as an image even when
 * it claims to be text/html, and `evil.html` never previews at all whatever it
 * claims to be.
 *
 * `none` is a first-class answer, not a failure. DOCX, XLSX, ZIP, TAR and
 * Parquet cannot be rendered by a browser and we ship no parser for them, so
 * their preview is an honest card naming the file. What `none` must never mean
 * again is "download it instead".
 */
export type PreviewKind = 'image' | 'pdf' | 'text' | 'none';
/** `unavailable` = the bytes are gone (a reload, or another device). */
export type ResolvedKind = PreviewKind | 'unavailable';

const IMAGE_BY_EXT: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  webp: 'image/webp',
  gif: 'image/gif',
};
const TEXT_EXT = new Set([
  'txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'jsonl', 'ndjson', 'log',
]);
const IMAGE_MIME = new Set(Object.values(IMAGE_BY_EXT));
const TEXT_MIME = new Set([
  'text/plain',
  'text/markdown',
  'text/csv',
  'text/tab-separated-values',
  'application/json',
]);

/**
 * Formats that EXECUTE wherever they are rendered. A blob: URL is its own
 * origin rather than ours, but "not quite our origin" is not a security model
 * worth betting a session on — these are never previewed, and (NEW-09A) never
 * downloaded either. They simply report that no preview exists.
 */
const NEVER_PREVIEW_EXT = new Set([
  'svg', 'svgz', 'html', 'htm', 'xhtml', 'xht', 'xml', 'js', 'mjs', 'cjs',
]);
const NEVER_PREVIEW_MIME = new Set([
  'image/svg+xml',
  'text/html',
  'application/xhtml+xml',
  'text/xml',
  'application/xml',
  'text/javascript',
  'application/javascript',
  'application/x-javascript',
  'application/ecmascript',
  'text/ecmascript',
]);

/**
 * How much of a text file the dialog will read.
 *
 * A dataset may be 200 MB. Reading it into a string to show the first screen
 * of it would freeze the tab, so the blob is SLICED before it is decoded and
 * the dialog says so. This changes nothing about what is uploaded.
 */
export const MAX_TEXT_PREVIEW_BYTES = 512 * 1024;

function extensionOf(name: string): string {
  const base = name.trim().toLowerCase();
  const dot = base.lastIndexOf('.');
  return dot === -1 ? '' : base.slice(dot + 1);
}

function normaliseMime(mime?: string | null): string {
  return (mime ?? '').split(';')[0].trim().toLowerCase();
}

/** "report.pdf" → "PDF", "sales.csv" → "CSV", "data.tar.gz" → "TAR.GZ". */
export function fileBadgeFor(name: string): string {
  const m = /\.(tar\.gz|[a-z0-9]{1,5})$/i.exec(name.trim());
  return m ? m[1].toUpperCase() : 'FILE';
}

/** Which renderer the preview dialog should use for this attachment. */
export function previewKindFor(name: string, mime?: string | null): PreviewKind {
  const ext = extensionOf(name);
  if (NEVER_PREVIEW_EXT.has(ext)) return 'none';
  if (IMAGE_BY_EXT[ext]) return 'image';
  if (ext === 'pdf') return 'pdf';
  if (TEXT_EXT.has(ext)) return 'text';
  // A known-but-unlisted extension (.docx, .zip, .xlsx) is answered by the
  // name alone; only a nameless file falls through to what it claims to be.
  if (ext) return 'none';
  const declared = normaliseMime(mime);
  if (NEVER_PREVIEW_MIME.has(declared)) return 'none';
  if (IMAGE_MIME.has(declared)) return 'image';
  if (declared === 'application/pdf') return 'pdf';
  if (TEXT_MIME.has(declared)) return 'text';
  return 'none';
}

/**
 * The type to stamp on the blob a preview renders, or null when nothing is
 * rendered. Forced from the allowlist, never copied from the file, so a
 * mislabelled upload cannot choose how it is displayed.
 *
 * Note what is NOT here: `application/octet-stream`. That value existed only
 * to make a browser save a file, and nothing saves files any more.
 */
export function previewMimeFor(
  name: string,
  mime?: string | null,
): string | null {
  const kind = previewKindFor(name, mime);
  if (kind === 'pdf') return 'application/pdf';
  if (kind === 'image') {
    // `||`, not `??`: an empty MIME is as absent as a missing one here.
    return IMAGE_BY_EXT[extensionOf(name)] || normaliseMime(mime) || null;
  }
  return null;
}

/** `data:image/png;base64,…` → the bytes, typed. null for anything else. */
export function dataUrlToBlob(dataUrl?: string | null): Blob | null {
  if (!dataUrl || !dataUrl.startsWith('data:')) return null;
  const comma = dataUrl.indexOf(',');
  if (comma === -1) return null;
  const header = dataUrl.slice(5, comma);
  if (!header.includes(';base64')) return null;
  const mime = header.split(';')[0] || 'application/octet-stream';
  try {
    const binary = atob(dataUrl.slice(comma + 1));
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: mime });
  } catch {
    return null;
  }
}

/** The declared type of a data: URL, without decoding its payload. */
export function mimeFromDataUrl(dataUrl?: string | null): string {
  if (!dataUrl || !dataUrl.startsWith('data:')) return '';
  const comma = dataUrl.indexOf(',');
  const header = comma === -1 ? dataUrl.slice(5) : dataUrl.slice(5, comma);
  return header.split(';')[0].trim().toLowerCase();
}

/**
 * Read the first `limit` bytes of a blob as text.
 *
 * `Blob.text()` is used when it exists and FileReader carries the rest: the
 * promise API is missing from older Safari and from jsdom, and FileReader —
 * which the composer already relies on to turn an upload into base64 — is
 * present everywhere. The blob is SLICED before it is decoded, so a 200 MB
 * dataset costs one screenful of memory rather than 200 MB of it.
 */
export function readBlobText(
  blob: Blob,
  limit: number = MAX_TEXT_PREVIEW_BYTES,
): Promise<string> {
  const slice = blob.slice(0, limit);
  if (typeof slice.text === 'function') return slice.text();
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(slice);
  });
}

export interface ResolvedAttachment {
  name: string;
  mime: string;
  /** null when the bytes are gone — after a reload, or on another device. */
  blob: Blob | null;
  size: number | null;
  kind: ResolvedKind;
}

/**
 * Everything the preview dialog needs about the Nth attachment of a message.
 *
 * `fallback.dataUrl` is the preview the MESSAGE persists. Images have one and
 * it is a real payload, so an image still previews after a reload. A document
 * does not — only its name survives — so this reports `unavailable` and the
 * dialog says so, rather than inventing bytes or opening an empty frame.
 */
export function resolveAttachment(
  messageId: string,
  index: number,
  fallback?: { name?: string; dataUrl?: string },
): ResolvedAttachment {
  const stored = attachmentFile(messageId, index);
  if (stored) {
    return {
      name: stored.name,
      mime: stored.mime,
      blob: stored.blob,
      size: stored.blob.size,
      kind: previewKindFor(stored.name, stored.mime),
    };
  }
  const name = fallback?.name ?? 'attachment';
  const blob = dataUrlToBlob(fallback?.dataUrl);
  if (blob) {
    return {
      name,
      mime: blob.type,
      blob,
      size: blob.size,
      kind: previewKindFor(name, blob.type),
    };
  }
  return { name, mime: '', blob: null, size: null, kind: 'unavailable' };
}

/* ==========================================================================
   NEW-10 / NEW-10A — reading a drag.

   The first version asked exactly one question — does `types` contain the
   string `Files`? — and, when the answer was no, returned without calling
   preventDefault. That is how a dragged file became `file:///…` typed into the
   composer: VS Code and several file managers describe a file as
   `text/uri-list` + `text/plain` and never say `Files` at all, so our handlers
   stood aside and the textarea did what a textarea does with dropped text.

   Sources disagree about which of `types`, `items` and `files` they populate,
   so all three are consulted, strongest evidence first, and a drop is
   classified once into a single intent the caller acts on.
   ========================================================================== */

/**
 * Is this drag carrying FILES?
 *
 * Asked on every drag event, because the answer decides whether we may take
 * the event over. Note the order: real `File` objects settle it outright;
 * `items[].kind` is the only witness available DURING a drag, when the bytes
 * are deliberately hidden from the page; `types` is the weakest and is checked
 * last precisely because trusting it alone was the bug.
 */
export function dragHasFiles(dt?: DataTransfer | null): boolean {
  if (!dt) return false;
  if (dt.files && dt.files.length > 0) return true;
  const items = dt.items ? Array.from(dt.items) : [];
  if (items.some((item) => item.kind === 'file')) return true;
  return Array.from(dt.types ?? []).includes('Files');
}

export interface DroppedFiles {
  files: File[];
  /** Dropped folders. Counted, not walked — see below. */
  directories: number;
}

/**
 * Identity for de-duplication.
 *
 * `items[i].getAsFile()` and `files[i]` may describe the same file through two
 * different File objects, so reference equality cannot be used. `lastModified`
 * is deliberately excluded: it is identical for two wrappers around one real
 * file, but differs for two `new File()` values built moments apart, which
 * would make this key useless in tests without making it safer in production.
 * Within a single drop, name + size + type is a sound identity.
 */
const fileKey = (f: File) => `${f.name}\u0000${f.size}\u0000${f.type}`;

/**
 * Every file in a drop, from both collections, with folders separated out.
 *
 * `items` is read first because it is the only place a directory can be
 * recognised, then `files` supplements it for sources that populate one and
 * not the other. The app has never uploaded folders and this is not the change
 * that adds it, so a dropped directory is counted and reported rather than
 * silently discarded (Chrome hands one over as a 0-byte File, which would
 * otherwise fail validation with a baffling complaint about its type).
 */
export function filesFromDrop(dt?: DataTransfer | null): DroppedFiles {
  const out: DroppedFiles = { files: [], directories: 0 };
  if (!dt) return out;
  const seen = new Set<string>();

  for (const item of dt.items ? Array.from(dt.items) : []) {
    if (item.kind !== 'file') continue;
    const entry = item.webkitGetAsEntry?.();
    const file = item.getAsFile();
    if (entry?.isDirectory) {
      out.directories += 1;
      // Claim its key so the `files` pass below cannot re-add it as a file.
      if (file) seen.add(fileKey(file));
      continue;
    }
    if (!file || seen.has(fileKey(file))) continue;
    seen.add(fileKey(file));
    out.files.push(file);
  }

  for (const file of Array.from(dt.files ?? [])) {
    if (seen.has(fileKey(file))) continue;
    seen.add(fileKey(file));
    out.files.push(file);
  }
  return out;
}

/**
 * URI schemes that name something on a machine rather than on the web.
 *
 * A page cannot read these — that is browser security, not an oversight, and
 * nothing here tries to work around it by fetching them or handing the path to
 * a server. They are recognised only so the drop can be refused honestly
 * instead of being pasted into the prompt as text.
 */
const LOCAL_URI_SCHEMES = [
  'file:',
  'vscode-file:',
  'vscode-remote:',
  'vscode-userdata:',
  'vscode-webview:',
  'content:',
];

/** text/uri-list is a multi-line format whose `#` lines are comments. */
function firstUriLine(value: string): string {
  return (
    value
      .split(/[\r\n]+/)
      .map((line) => line.trim())
      .find((line) => line && !line.startsWith('#')) ?? ''
  );
}

/**
 * Does this dragged text name a local file rather than a web resource?
 *
 * Deliberately conservative. Over-claiming here would break ordinary text and
 * link dragging, which must keep working everywhere in the app, so a bare path
 * has to look like a path — no whitespace — before it counts.
 */
export function isFileLikeUri(value: string): boolean {
  const first = firstUriLine(value);
  if (!first) return false;
  const lower = first.toLowerCase();
  if (LOCAL_URI_SCHEMES.some((scheme) => lower.startsWith(scheme))) return true;
  if (/\s/.test(first)) return false; // prose, not a path
  // A Windows drive letter parses as a URI scheme, so it is tested first.
  if (/^[a-zA-Z]:[\\/]/.test(first)) return true;
  if (/^\/[^/]/.test(first)) return true; // /home/user/report.pdf
  return first.startsWith('\\\\'); // \\server\share
}

function dragText(dt: DataTransfer): string {
  if (typeof dt.getData !== 'function') return '';
  try {
    return dt.getData('text/uri-list') || dt.getData('text/plain') || '';
  } catch {
    // getData throws outside a drop in some browsers; nothing to read.
    return '';
  }
}

/**
 * What a drop actually is, decided once.
 *
 * `ignore` is the only outcome the caller must NOT preventDefault on — it means
 * an ordinary text or web-link drag, whose default behaviour belongs to the
 * browser. Everything else is ours, which is what stops a URI reaching the
 * textarea.
 */
export type DropIntent =
  | { action: 'files'; files: File[]; directories: number }
  | { action: 'directories'; directories: number }
  | { action: 'file-uri'; uri: string }
  | { action: 'ignore' };

export function dropIntent(dt?: DataTransfer | null): DropIntent {
  if (!dt) return { action: 'ignore' };
  const { files, directories } = filesFromDrop(dt);
  // Real bytes always win. A source that offers both a File and a link to it
  // is offering one file twice, and the File is the half we can actually use.
  if (files.length) return { action: 'files', files, directories };
  if (directories > 0) return { action: 'directories', directories };

  const text = dragText(dt);
  if (!text) return { action: 'ignore' };
  if (isFileLikeUri(text)) return { action: 'file-uri', uri: text };
  // A drag that ADVERTISED files and then produced none: whatever link it also
  // carried stands in for the file, so it is not text the user meant to type.
  if (dragHasFiles(dt)) return { action: 'file-uri', uri: text };
  return { action: 'ignore' };
}

/** Test seam. */
export function clearAttachments(): void {
  sent.clear();
  held.clear();
}
