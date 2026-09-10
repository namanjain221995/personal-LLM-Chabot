/**
 * Streaming document upload (2026-09-02), made RESUMABLE (2026-09-10).
 *
 * Documents used to travel as base64 inside the chat JSON, which is what
 * capped them at 25 MB — the limit belonged to the transport, not to
 * anything the engine needs. Big documents now stream to the same rail
 * datasets use (`purpose=document`: the server keeps the original bytes and
 * extracts nothing), and the chat request carries a reference.
 *
 * Cloudflare's edge caps a single request body at 100 MB on this plan, so a
 * file bigger than CHUNK_THRESHOLD_BYTES is sliced into CHUNK_PART_BYTES
 * pieces and reassembled server-side — that is what makes a 512 MB upload
 * work on ai.techsarasolutions.com and not just on the LAN.
 *
 * WHAT 2026-09-10 ADDED, AND WHY.
 *
 * The old client sent every part exactly once and surfaced the first failure
 * as a rejected promise. One dropped connection at part 6 of 7 therefore
 * threw away five parts the server was still holding, and the caller's
 * `.catch(() => null)` re-uploaded the whole file from byte 0 under a new
 * session (fe-attach F6). It also never told the server how big the file was,
 * so a missing FINAL part assembled silently into a short file (F10, T-04).
 *
 * So: `init` declares size/parts/part_size, every part carries its SHA-256,
 * a transient failure asks the server what it already has
 * (`GET /uploads/chunked/{conv}/{id}`) and sends only the rest, and the
 * caller can resume a session it recorded earlier. Retries are bounded and
 * apply ONLY to failures that say nothing about the request (a network drop,
 * 429, 502/503/504); a refusal — 400, 401, 403, 404, 409, 413, 415, 422 — is
 * final by definition and is surfaced verbatim, because "part exceeds the
 * limit" is something the person can act on and retrying it is a lie.
 *
 * See docs/upload-reliability/API.md for the server half of this contract.
 */

import { formatBytes } from './format';
import type { AttachmentUploadState } from './types';

export const CHUNK_THRESHOLD_BYTES = 90 * 1024 * 1024;
export const CHUNK_PART_BYTES = 64 * 1024 * 1024;

/** Five attempts, 0.5 s base, 8 s cap — see the retry note above. */
const MAX_ATTEMPTS = 5;
const RETRY_BASE_MS = 500;
const RETRY_CAP_MS = 8000;
/**
 * The statuses that carry no verdict about the request itself: the proxy or
 * the orchestrator was momentarily unable to answer (502/503/504), or asked
 * us to slow down (429). Everything else — including 409, which means the
 * session has moved on — is a decision, and a decision is not retried.
 */
const RETRYABLE_STATUS = new Set([429, 502, 503, 504]);

export type UploadPurpose = 'document' | 'video';

/**
 * The three upload states this client can OBSERVE. They are the same words
 * the persisted attachment uses, deliberately: an attachment's `upload_state`
 * is written from these, and `uploaded` is only ever declared by the server
 * answering (see docs/upload-reliability/CONTRACT.md).
 */
export type UploadProgressState = Extract<
  AttachmentUploadState,
  'uploading' | 'finalizing' | 'uploaded'
>;

export interface UploadProgress {
  state: UploadProgressState;
  /** Bytes the SERVER has confirmed, never bytes handed to fetch. */
  bytesSent: number;
  bytesTotal: number;
  partsDone: number;
  partsTotal: number;
  /**
   * Chunked only: the server's session id, known from `init` onwards. Its
   * presence is what tells a caller this upload can be resumed at all — a
   * single-shot POST has no session and no partial state to return to.
   */
  sessionId?: string;
  /** Echoed from the options, so a caller juggling several uploads can route
      a progress event without closing over the chip it belongs to. */
  attachmentId?: string;
}

export interface DocumentRef {
  upload_id: string;
  name: string;
  /** What the server stored, when it says so; the file's own size otherwise.
      A re-selected file is checked against this before a resume sends a byte. */
  bytes: number;
}

export interface UploadOptions {
  /** The browser-minted identity of the attachment these bytes belong to. */
  attachmentId?: string;
  signal?: AbortSignal;
  onProgress?: (progress: UploadProgress) => void;
  /** Continue a chunked session this browser already started. */
  resume?: { uploadId: string };
}

/**
 * A failure with everything the UI needs to say something true about it.
 *
 * `status` is 0 when no response was ever received (a dropped connection),
 * which is NOT the same as a server saying nothing — hence a distinct value
 * rather than an optional field.
 */
export class UploadError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly retryable: boolean;
  readonly sessionId?: string;

  constructor(init: {
    status: number;
    message: string;
    retryable: boolean;
    code?: string;
    sessionId?: string;
  }) {
    super(init.message);
    this.name = 'UploadError';
    this.status = init.status;
    this.code = init.code;
    this.retryable = init.retryable;
    this.sessionId = init.sessionId;
  }
}

/** A JSON error body, as far as anything here relies on its shape. */
interface ErrorBody {
  detail?: unknown;
  message?: unknown;
  code?: unknown;
}

/** What the upload endpoints answer with on success. */
interface UploadBody {
  upload_id?: unknown;
  filename?: unknown;
  bytes?: unknown;
}

/** `GET /uploads/chunked/{conv}/{id}` — what the server already has. */
interface SessionBody extends UploadBody {
  status?: unknown;
  expected_bytes?: unknown;
  accepted_parts?: unknown;
  bytes_received?: unknown;
  expected_parts?: unknown;
  part_size?: unknown;
  result?: unknown;
}

function abortError(): Error {
  // Named, not typed: `AbortError` is how every caller in this codebase (and
  // the platform) recognises "the person cancelled" as distinct from a failure.
  const err = new Error('The upload was cancelled.');
  err.name = 'AbortError';
  return err;
}

function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw abortError();
}

function isAbort(err: unknown, signal?: AbortSignal): boolean {
  if (signal?.aborted) return true;
  return err instanceof Error && err.name === 'AbortError';
}

/**
 * A sentence that names the status and nothing else.
 *
 * A proxy answers with an HTML page, and a tab that renders "<!DOCTYPE html>"
 * where a reason belongs tells the person nothing and looks broken. Every
 * non-JSON body becomes one of these instead.
 */
function safeMessage(what: string, status: number): string {
  if (status === 0) return `${what} could not reach the server.`;
  if (status === 429) return `${what} was throttled by the server (HTTP 429).`;
  if (status >= 500) {
    return `${what} failed — the upload service is temporarily unavailable (HTTP ${status}).`;
  }
  return `${what} was refused by the server (HTTP ${status}).`;
}

/** Wait, but let an abort cut the wait short — a cancelled upload must not
    sit through an 8-second backoff before it stops. */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

/** Full jitter on top of the doubling, so a hundred tabs that lost the same
    proxy do not come back in lockstep. */
function backoffMs(attempt: number): number {
  const window = Math.min(RETRY_CAP_MS, RETRY_BASE_MS * 2 ** (attempt - 1));
  return Math.round(window * (0.5 + Math.random() * 0.5));
}

/** Turn a failed response into the error the UI will show. Reads the body
    ONCE — a Response body cannot be consumed twice. */
async function errorFromResponse(
  res: Response,
  what: string,
  sessionId?: string,
): Promise<UploadError> {
  let raw = '';
  try {
    raw = await res.text();
  } catch {
    /* the connection died mid-body; the status still carries the story */
  }
  let detail: string | null = null;
  let code: string | undefined;
  if (raw) {
    try {
      const body = JSON.parse(raw) as ErrorBody;
      if (body && typeof body === 'object') {
        if (typeof body.detail === 'string') detail = body.detail;
        else if (typeof body.message === 'string') detail = body.message;
        if (typeof body.code === 'string') code = body.code;
      }
    } catch {
      /* an HTML error page from a proxy — safeMessage below names the status */
    }
  }
  return new UploadError({
    status: res.status,
    message: detail ?? safeMessage(what, res.status),
    retryable: RETRYABLE_STATUS.has(res.status),
    code,
    sessionId,
  });
}

interface RequestOptions {
  signal?: AbortSignal;
  sessionId?: string;
  /** Called with the response before its body is read — the moment the
      request's bytes are all on the server and it starts working on them. */
  onResponse?: () => void;
}

/**
 * One request, retried while — and only while — the failure is transient.
 *
 * `send` is a factory rather than a Request because a retry needs a fresh
 * one: a FormData or Blob body may be re-read, but a Response's has already
 * been consumed by the attempt that failed.
 */
async function requestJson<T>(
  send: () => Promise<Response>,
  what: string,
  options: RequestOptions = {},
): Promise<T> {
  const { signal, sessionId } = options;
  let last: UploadError | null = null;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
    throwIfAborted(signal);
    let res: Response;
    try {
      res = await send();
    } catch (err) {
      if (isAbort(err, signal)) throw abortError();
      // A fetch that THROWS answered nothing at all: DNS, a dropped socket,
      // a proxy that hung up. Nothing is known about the request, which is
      // exactly the case a retry exists for.
      last = new UploadError({
        status: 0,
        message: safeMessage(what, 0),
        retryable: true,
        code: 'network',
        sessionId,
      });
      if (attempt === MAX_ATTEMPTS) throw last;
      await sleep(backoffMs(attempt), signal);
      continue;
    }
    options.onResponse?.();
    if (!res.ok) {
      last = await errorFromResponse(res, what, sessionId);
      if (!last.retryable || attempt === MAX_ATTEMPTS) throw last;
      await sleep(backoffMs(attempt), signal);
      continue;
    }
    try {
      return (await res.json()) as T;
    } catch {
      // 200 with a body we cannot read is not a success we can act on: the
      // upload id is the entire point of the response.
      throw new UploadError({
        status: res.status,
        message: safeMessage(what, res.status),
        retryable: false,
        code: 'unreadable_body',
        sessionId,
      });
    }
  }
  // Unreachable: every path through the loop returns or throws. Kept because
  // TypeScript cannot see that, and a bare `undefined` escaping from here
  // would be worse than a sentence.
  throw last ?? new UploadError({ status: 0, message: safeMessage(what, 0), retryable: true });
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function asNumber(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

/** The accepted-part list, defensively: this drives which bytes are skipped. */
function acceptedParts(value: unknown): Set<number> {
  const out = new Set<number>();
  if (!Array.isArray(value)) return out;
  for (const entry of value) {
    if (typeof entry === 'number' && Number.isInteger(entry) && entry >= 0) {
      out.add(entry);
    }
  }
  return out;
}

function refFrom(body: UploadBody, file: { name: string; size: number }): DocumentRef {
  return {
    upload_id: asString(body.upload_id),
    name: asString(body.filename, file.name) || file.name,
    bytes: asNumber(body.bytes, file.size),
  };
}

const chunkedBase = (conversationId: string, uploadId: string) =>
  `/api/upload/chunked/${encodeURIComponent(conversationId)}/${encodeURIComponent(uploadId)}`;

/**
 * The SHA-256 of one part, as the server will recompute it.
 *
 * `crypto.subtle` is undefined outside a secure context — a plain-http LAN
 * address that is not localhost — so the header is OMITTED there rather than
 * failing the upload: the server treats `X-Part-SHA256` as optional and only
 * checks what it is given. The part is read into memory to hash it, which is
 * why parts are hashed one at a time and never the whole file.
 */
async function partHash(part: Blob): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle || typeof part.arrayBuffer !== 'function') return null;
  try {
    const digest = await subtle.digest('SHA-256', await part.arrayBuffer());
    return Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('');
  } catch {
    // A browser that has subtle but refuses SHA-256 is not a thing we can
    // fix here, and the header is optional. Send the part without it.
    return null;
  }
}

async function uploadSingle(
  file: File,
  conversationId: string,
  purpose: UploadPurpose,
  opts: UploadOptions,
): Promise<DocumentRef> {
  const report = (state: UploadProgressState, bytesSent: number) =>
    opts.onProgress?.({
      state,
      bytesSent,
      bytesTotal: file.size,
      partsDone: state === 'uploaded' ? 1 : 0,
      partsTotal: 1,
      attachmentId: opts.attachmentId,
    });

  // No invented percentage. One request either arrives or it does not, and a
  // bar that crawls to 90% and waits is a lie the person then distrusts.
  report('uploading', 0);
  const body = await requestJson<UploadBody>(
    () => {
      const form = new FormData();
      form.append('file', file);
      form.append('conversation_id', conversationId);
      form.append('purpose', purpose);
      return fetch('/api/upload', { method: 'POST', body: form, signal: opts.signal });
    },
    'The upload',
    {
      signal: opts.signal,
      // The response's headers arriving means the bytes are all there and the
      // server has started reading them.
      onResponse: () => report('finalizing', file.size),
    },
  );
  report('uploaded', file.size);
  return refFrom(body, file);
}

interface ChunkedSession {
  uploadId: string;
  accepted: Set<number>;
  /** Present once the session is `complete` — the finalisation response. */
  result: UploadBody | null;
  status: string;
  /** What the session was opened for, when `init` was told; null otherwise. */
  expectedBytes: number | null;
  filename: string;
}

async function uploadChunked(
  file: File,
  conversationId: string,
  purpose: UploadPurpose,
  opts: UploadOptions,
): Promise<DocumentRef> {
  const partsTotal = Math.max(1, Math.ceil(file.size / CHUNK_PART_BYTES));
  const partBytes = (index: number) =>
    Math.max(0, Math.min(CHUNK_PART_BYTES, file.size - index * CHUNK_PART_BYTES));

  let session: ChunkedSession;
  if (opts.resume?.uploadId) {
    session = await loadSession(conversationId, opts.resume.uploadId, opts.signal);
    // A resume needs THE SAME BYTES (CONTRACT.md). Sending this file's parts
    // into a session opened for another one would assemble a file nobody
    // uploaded, so the size is checked before a byte moves. Only the size:
    // the filename the server reports has been through its own sanitising,
    // and refusing a legitimate resume over a renamed space would be worse
    // than the risk it removes.
    if (session.expectedBytes !== null && session.expectedBytes !== file.size) {
      throw new UploadError({
        status: 409,
        message: `That upload was started for a different file (${formatBytes(
          session.expectedBytes,
        )}, not ${formatBytes(file.size)}). Attach ${file.name} again to start a new one.`,
        retryable: false,
        code: 'session_mismatch',
        sessionId: session.uploadId,
      });
    }
  } else {
    const init = await requestJson<SessionBody>(
      () => {
        const form = new FormData();
        form.append('conversation_id', conversationId);
        form.append('filename', file.name);
        form.append('purpose', purpose);
        // T-04/F10: without these the server can only check that the parts it
        // holds are contiguous, so a missing FINAL part assembles into a
        // shorter file that looks fine. Declared here, refused there.
        form.append('size', String(file.size));
        form.append('parts', String(partsTotal));
        form.append('part_size', String(CHUNK_PART_BYTES));
        return fetch('/api/upload/chunked/init', {
          method: 'POST',
          body: form,
          signal: opts.signal,
        });
      },
      'Starting the upload',
      { signal: opts.signal },
    );
    session = {
      uploadId: asString(init.upload_id),
      accepted: acceptedParts(init.accepted_parts),
      result: null,
      status: asString(init.status, 'uploading'),
      expectedBytes: file.size,
      filename: file.name,
    };
  }

  const report = (state: UploadProgressState) => {
    let bytesSent = 0;
    for (const index of session.accepted) bytesSent += partBytes(index);
    opts.onProgress?.({
      state,
      bytesSent: state === 'uploaded' ? file.size : Math.min(bytesSent, file.size),
      bytesTotal: file.size,
      partsDone: state === 'uploaded' ? partsTotal : session.accepted.size,
      partsTotal,
      sessionId: session.uploadId,
      attachmentId: opts.attachmentId,
    });
  };

  // A session the server has already finalised is the whole answer: replaying
  // `complete` would be harmless (it is idempotent) but asking at all is not
  // needed, and the stored result is exactly what the first caller received.
  if (session.result) {
    report('uploaded');
    return refFrom(session.result, file);
  }

  report('uploading');

  const sendPart = async (index: number) => {
    const slice = file.slice(index * CHUNK_PART_BYTES, (index + 1) * CHUNK_PART_BYTES);
    const hash = await partHash(slice);
    const body = await requestJson<SessionBody>(
      () =>
        fetch(`${chunkedBase(conversationId, session.uploadId)}/part/${index}`, {
          method: 'PUT',
          headers: hash ? { 'X-Part-SHA256': hash } : undefined,
          body: slice,
          signal: opts.signal,
        }),
      `Part ${index + 1} of ${partsTotal}`,
      { signal: opts.signal, sessionId: session.uploadId },
    );
    // The server's own list wins over our bookkeeping: it is the thing
    // `complete` will check. An older server that answers without one leaves
    // us to record the part ourselves.
    const accepted = acceptedParts(body.accepted_parts);
    if (accepted.size) session.accepted = accepted;
    else session.accepted.add(index);
    report('uploading');
  };

  // Two passes at most. The first sends what is missing; if a transient
  // failure survives its own retries, the session is re-read (the server may
  // well have accepted the part whose ACK we lost) and only what is still
  // missing goes out again.
  for (let pass = 0; pass < 2; pass += 1) {
    try {
      for (let index = 0; index < partsTotal; index += 1) {
        throwIfAborted(opts.signal);
        if (session.accepted.has(index)) continue;
        await sendPart(index);
      }
      break;
    } catch (err) {
      if (pass === 1 || !(err instanceof UploadError) || !err.retryable) throw err;
      const refreshed = await loadSession(conversationId, session.uploadId, opts.signal);
      session.accepted = refreshed.accepted;
      session.status = refreshed.status;
      if (refreshed.result) {
        report('uploaded');
        return refFrom(refreshed.result, file);
      }
      report('uploading');
    }
  }

  throwIfAborted(opts.signal);
  report('finalizing');
  const done = await requestJson<UploadBody>(
    () =>
      fetch(`${chunkedBase(conversationId, session.uploadId)}/complete`, {
        method: 'POST',
        signal: opts.signal,
      }),
    'Finishing the upload',
    { signal: opts.signal, sessionId: session.uploadId },
  );
  report('uploaded');
  return refFrom(done, file);
}

/** Ask the server what it already holds for a session. */
async function loadSession(
  conversationId: string,
  uploadId: string,
  signal?: AbortSignal,
): Promise<ChunkedSession> {
  const body = await requestJson<SessionBody>(
    () => fetch(chunkedBase(conversationId, uploadId), { method: 'GET', signal }),
    'Checking the upload',
    { signal, sessionId: uploadId },
  );
  const status = asString(body.status, 'uploading');
  const result =
    body.result && typeof body.result === 'object' ? (body.result as UploadBody) : null;
  if (status === 'expired' || status === 'cancelled') {
    // Terminal by the server's own account: the parts are gone, so there is
    // nothing to resume and re-sending them would be a new upload anyway.
    throw new UploadError({
      status: 409,
      message:
        status === 'expired'
          ? 'That upload expired before it finished — attach the file again.'
          : 'That upload was cancelled.',
      retryable: false,
      code: status,
      sessionId: uploadId,
    });
  }
  const expected = body.expected_bytes;
  return {
    uploadId: asString(body.upload_id, uploadId) || uploadId,
    accepted: acceptedParts(body.accepted_parts),
    result,
    status,
    expectedBytes:
      typeof expected === 'number' && Number.isFinite(expected) ? expected : null,
    filename: asString(body.filename),
  };
}


/**
 * Give a chunked session back to the server.
 *
 * Aborting the fetch stops THIS tab sending; it does not tell the server that
 * the parts already on disk will never be completed (fe-attach F7: a chip the
 * person removed kept its bytes, and for a video its analysis, alive). This
 * says so. Best-effort by design: it is called from a removal that has
 * already happened, so a failure here costs nothing — the session's own TTL
 * sweep reclaims the parts — and it never throws into that path.
 */
export async function cancelChunkedUpload(
  conversationId: string,
  uploadId: string,
): Promise<void> {
  try {
    await fetch(chunkedBase(conversationId, uploadId), { method: 'DELETE' });
  } catch {
    /* offline, or the tab is closing: the TTL sweep is the backstop */
  }
}

/**
 * Stream one document to the server; → the reference the chat request sends.
 *
 * 2026-09-09: a video takes the same road with purpose=video — the server
 * keeps the bytes and starts its analysis behind the response.
 * 2026-09-10: `opts` carries the identity, the cancellation and the progress
 * the composer needs, and a resumable session's id. The positional arguments
 * are unchanged, so every existing caller keeps working untouched.
 */
export async function uploadDocumentFile(
  file: File,
  conversationId: string,
  purpose: UploadPurpose = 'document',
  opts: UploadOptions = {},
): Promise<DocumentRef> {
  throwIfAborted(opts.signal);
  // A resume is always a chunked session, whatever the file's size says.
  return file.size > CHUNK_THRESHOLD_BYTES || opts.resume
    ? uploadChunked(file, conversationId, purpose, opts)
    : uploadSingle(file, conversationId, purpose, opts);
}
