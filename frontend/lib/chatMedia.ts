/**
 * Chat media: the photos of a conversation, stored on our server
 * (2026-10-02, docs/chat-media/CONTRACT.md §7 and §10).
 *
 * The defect this exists for: a photo sent from a phone showed on the phone
 * and nowhere else. Its only lasting copy was `imageDataUrl` in the sending
 * browser's IndexedDB; server history carried role, content and meta, and
 * meta said nothing about the photo. The orchestrator now keeps every photo it
 * is sent (one row per photo, bytes on disk) and this module is the browser's
 * half of that:
 *
 *   · what a user turn records about its photos (`meta.images`, written at
 *     send, never volatile — the history sync key hashes all of meta);
 *   · where a stored photo is shown from (a URL DERIVED from the reference,
 *     never stored, so a deployment can move the route without a migration);
 *   · how a thumbnail reserves its box before a byte arrives (no layout
 *     shift on a device that has never seen the photo);
 *   · the BACKFILL, which uploads the photos of older turns from the one
 *     browser that still holds them — the server holds no copy of anything
 *     sent before this change, so that browser is the only way back.
 *
 * Nothing here runs on the send's critical path. The bytes of a new photo
 * still travel inline in the /chat body (and the server stores them from
 * there, `image_ids`); the only thing a send gains is a few short strings in
 * meta.
 */

import type { ChatMessage, MessageImage, Meta } from './types';
import { dataUrlToBlob, mimeFromDataUrl } from './attachments';
import { turnFingerprint, type HeldImageRecord } from './idbCache';

/** An attachment id as the server accepts it (CONTRACT §3, client-minted). */
export const ATTACHMENT_ID = /^[A-Za-z0-9_-]{8,64}$/;
/** A conversation id as every proxy in this app validates it. */
export const CONVERSATION_ID = /^[A-Za-z0-9_-]{1,64}$/;
/**
 * F034: `u<digits>-` keys are the orchestrator's bare /chat sessions, never a
 * history conversation, and every byte route refuses them. Nothing in a chat
 * the person owns is ever under one, so the backfill does not even ask.
 */
const RESERVED_CONVERSATION = /^u\d+-/;

export type MediaSize = 'thumb' | 'full';

/** Where one stored photo lives: the viewer's conversation and its id. */
export interface MediaRef {
  conversationId: string;
  attachmentId: string;
}

export function isAttachmentId(value: unknown): value is string {
  return typeof value === 'string' && ATTACHMENT_ID.test(value);
}

/**
 * The same-origin URL a stored photo is shown from. Derived on every render
 * and never persisted (CONTRACT §7): the reference is the identity, the URL
 * is only today's way of reaching it.
 */
export function chatMediaUrl(ref: MediaRef, size: MediaSize): string {
  return `/api/chat-media/${encodeURIComponent(ref.conversationId)}/${encodeURIComponent(
    ref.attachmentId,
  )}?size=${size}`;
}

/* ------------------------------------------------------- what a turn says */

/** The composer-side facts one sent photo contributes to `meta.images`. */
export interface SentImage {
  attachment_id?: string;
  name?: string;
  /** The data URL that was sent; its declared type is the stored type. */
  dataUrl?: string;
  width?: number;
  height?: number;
}

function positiveInt(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) && value > 0
    ? value
    : undefined;
}

/**
 * The `meta.images` a send writes, in the order the photos are SENT.
 *
 * Only facts that never change go in: the id, the name the person picked, the
 * type the bytes were sent as and their pixel size. `undefined` fields are
 * left out rather than written as such, so the object is the same shape every
 * time it is built — the sync key is a hash of canonical JSON, and a key that
 * appeared and disappeared would re-push the thread for nothing.
 *
 * A photo without a well-formed id is skipped: the server could not have
 * stored it, and a reference that can never resolve would only render as
 * "unavailable" on every other device. `undefined` when nothing remains, so a
 * text-only turn's meta keeps exactly the key set it always had.
 */
export function imagesMetaFor(images: SentImage[]): MessageImage[] | undefined {
  const out: MessageImage[] = [];
  for (const image of images) {
    if (!isAttachmentId(image.attachment_id)) continue;
    const mime = image.dataUrl ? mimeFromDataUrl(image.dataUrl) : '';
    const width = positiveInt(image.width);
    const height = positiveInt(image.height);
    out.push({
      attachment_id: image.attachment_id,
      ...(image.name ? { name: image.name } : {}),
      ...(mime ? { mime } : {}),
      ...(width && height ? { width, height } : {}),
    });
  }
  return out.length ? out : undefined;
}

/**
 * A user turn's meta with its photo references added — `meta` untouched when
 * there are none, so a turn without photos keeps exactly the meta (and the
 * sync key) it always had.
 */
export function withImagesMeta(
  meta: Meta | undefined,
  images: MessageImage[] | undefined,
): Meta | undefined {
  if (!images?.length) return meta;
  return { ...(meta ?? {}), images };
}

/**
 * The well-formed stored-photo references on a turn, in order. Structural, so
 * the resend path (and tests) need no full ChatMessage.
 */
export function storedImagesOf(message: {
  meta?: { images?: ReadonlyArray<Partial<MessageImage> | null> } | null;
}): MessageImage[] {
  const images = message.meta?.images;
  if (!Array.isArray(images)) return [];
  return images.filter((image): image is MessageImage =>
    isAttachmentId(image?.attachment_id),
  );
}

/** The photos this browser holds for a turn, as data URLs, in order. */
export function localImagesOf(
  message: Pick<ChatMessage, 'imageDataUrl' | 'imageDataUrls'>,
): string[] {
  if (message.imageDataUrls?.length) return message.imageDataUrls;
  return message.imageDataUrl ? [message.imageDataUrl] : [];
}

function hasPhotos(message: ChatMessage): boolean {
  return localImagesOf(message).length > 0 || storedImagesOf(message).length > 0;
}

/**
 * Should this turn say that its photo was never stored?
 *
 * A turn sent before photos were saved has no `meta.images`, and on any device
 * but the one that sent it no local bytes either: it rendered as a bare
 * question under an answer about a picture nobody can see. When the answer
 * that follows was the vision route's (the hint the production row 16136
 * carries), one muted line says what happened instead of nothing. An image
 * sent with a document routes to the document engine, so this stays silent
 * there rather than guess.
 *
 * A vision answer is not proof of a photo, though: a text follow-up ABOUT an
 * earlier photo ("what colour is the stem?") is answered by the vision engine
 * too (orchestrator image memory, main.py `image_followup_images`), with the
 * same `route: 'vision'`. So once the thread before this turn (`earlier`, in
 * reading order) holds a photo or a vision answer, a photo-less turn under a
 * vision answer is taken for such a follow-up and gets no line.
 */
export function showsLegacyPhotoNote(
  message: ChatMessage,
  next: ChatMessage | undefined,
  earlier: readonly ChatMessage[] = [],
): boolean {
  if (message.role !== 'user') return false;
  if (hasPhotos(message)) return false;
  if (next?.role !== 'assistant' || next.meta?.route !== 'vision') return false;
  return !earlier.some((m) =>
    m.role === 'user' ? hasPhotos(m) : m.meta?.route === 'vision',
  );
}

/**
 * The one turn of a thread (as read, in order) that gets the legacy line, or
 * null — `showsLegacyPhotoNote` for every turn in a single pass.
 */
export function legacyPhotoNoteId(thread: readonly ChatMessage[]): string | null {
  for (let i = 0; i < thread.length; i += 1) {
    const m = thread[i];
    if (m.role === 'user' && hasPhotos(m)) return null;
    if (m.role === 'assistant' && m.meta?.route === 'vision') return null;
    if (showsLegacyPhotoNote(m, thread[i + 1])) return m.id;
  }
  return null;
}

/**
 * An image's pixel size as the browser draws it (EXIF orientation applied),
 * or null when it cannot be decoded here — no DOM (tests, SSR), an
 * undecodable file, or a decode slower than `timeoutMs`. Never throws, never
 * waits long: the size is a nicety for other devices, not a gate on sending.
 */
export function measureDataUrl(
  dataUrl: string,
  timeoutMs = 3_000,
): Promise<{ width: number; height: number } | null> {
  if (typeof Image === 'undefined' || !dataUrl.startsWith('data:image/')) {
    return Promise.resolve(null);
  }
  return new Promise((resolve) => {
    const img = new Image();
    let settled = false;
    const finish = (size: { width: number; height: number } | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(size);
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    img.onload = () => {
      const width = positiveInt(img.naturalWidth);
      const height = positiveInt(img.naturalHeight);
      finish(width && height ? { width, height } : null);
    };
    img.onerror = () => finish(null);
    img.decoding = 'async';
    img.src = dataUrl;
  });
}

/* ------------------------------------------------------- the thumbnail box */

/** The bubble's thumbnail height: the `max-h-40` local previews already use. */
export const THUMB_MAX_HEIGHT_PX = 160;

/**
 * The box a stored thumbnail occupies BEFORE it loads, from the pixel size the
 * sending browser recorded — the same size the local data-URL preview settles
 * at (`max-h-40`, width from the aspect ratio), so the sender and every other
 * device draw the same bubble.
 *
 * A pixel width capped at 100 % plus an aspect ratio, never a fixed height:
 * when a narrow screen caps the width, the height follows the ratio instead
 * of cropping or stretching. null when the turn recorded no size (a legacy or
 * odd row) — the caller then uses a fixed square, which cannot shift either.
 */
export function thumbBox(
  image: Pick<MessageImage, 'width' | 'height'>,
): { width: string; maxWidth: string; height: string; aspectRatio: string } | null {
  const width = positiveInt(image.width);
  const height = positiveInt(image.height);
  if (!width || !height) return null;
  const shown = Math.min(THUMB_MAX_HEIGHT_PX, height);
  const across = Math.max(1, Math.round((shown * width) / height));
  return {
    width: `${across}px`,
    maxWidth: '100%',
    height: 'auto',
    aspectRatio: `${width} / ${height}`,
  };
}

/* ------------------------------------------------------- reading the bytes */

export type MediaFetchOutcome =
  | { status: 'ok'; blob: Blob }
  /** 404 or 410: the server has no such photo, or no longer has its file. */
  | { status: 'missing' }
  /** Offline, aborted, or the service failing — nothing was decided. */
  | { status: 'unavailable' };

/**
 * One stored photo's bytes, for the preview dialog.
 *
 * The HTTP cache is used, not bypassed: the route answers `private,
 * immutable` with an ETag, because an attachment id never changes content
 * (first write wins, CONTRACT §3), so a photo opened once opens again from
 * disk.
 */
export async function fetchChatMediaBlob(
  ref: MediaRef,
  size: MediaSize,
  signal?: AbortSignal,
): Promise<MediaFetchOutcome> {
  try {
    const res = await fetch(chatMediaUrl(ref, size), { signal });
    if (res.status === 404 || res.status === 410) return { status: 'missing' };
    if (!res.ok) return { status: 'unavailable' };
    return { status: 'ok', blob: await res.blob() };
  } catch {
    return { status: 'unavailable' };
  }
}

/**
 * The attachment ids the server holds for this viewer's conversation
 * (GET /api/chat-media/{conversation}), or null when that could not be
 * learned — offline, a failing service, an answer of the wrong shape. null
 * never means "none": callers that act on a missing id act only on a list.
 */
export async function storedAttachmentIds(
  conversationId: string,
  signal?: AbortSignal,
): Promise<Set<string> | null> {
  try {
    const res = await fetch(`/api/chat-media/${encodeURIComponent(conversationId)}`, {
      signal,
      cache: 'no-store',
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { items?: unknown };
    if (!Array.isArray(body.items)) return null;
    const ids = new Set<string>();
    for (const item of body.items as Array<{ attachment_id?: unknown } | null>) {
      if (isAttachmentId(item?.attachment_id)) ids.add(item.attachment_id);
    }
    return ids;
  } catch {
    return null;
  }
}

/* ------------------------------------------------------- uploading photos */

/** One photo in a POST /api/chat-media/{conversation} batch. */
export interface MediaUploadPart {
  attachmentId: string;
  blob: Blob;
  name: string;
}

/** What the server says it holds after an upload (CONTRACT §4.1). */
export interface StoredMediaItem {
  attachment_id: string;
  mime?: string;
  width?: number | null;
  height?: number | null;
}

export type MediaUploadOutcome =
  | { kind: 'stored'; items: StoredMediaItem[] }
  /** 400 / 413 / 415: these bytes will never be accepted. Do not retry. */
  | { kind: 'refused'; status: number }
  /** 404: not this person's conversation (or not one at all). */
  | { kind: 'not_found' }
  /** 401 / 403: the session is gone; nothing more can be done here. */
  | { kind: 'unauthenticated' }
  /** 507: the server is below its free-space floor. */
  | { kind: 'no_space' }
  /** Anything else the server said — try another time. */
  | { kind: 'failed'; status: number }
  /** The request never got an answer. */
  | { kind: 'offline' };

/** At most this many photos per request, the composer's and the server's cap. */
export const MAX_MEDIA_PER_REQUEST = 5;

/**
 * Upload up to five photos to the conversation's media store.
 *
 * Multipart, one `file` part and one `attachment_id` part per photo in the
 * same order (the server pairs them by position and refuses a count
 * mismatch). Idempotent by contract: an id the server already holds comes
 * back unchanged with `created: false`, so a retry after a lost answer costs
 * one round trip and stores nothing twice.
 */
export async function uploadChatMedia(
  conversationId: string,
  parts: MediaUploadPart[],
  source: 'upload' | 'backfill',
  signal?: AbortSignal,
): Promise<MediaUploadOutcome> {
  const form = new FormData();
  for (const part of parts.slice(0, MAX_MEDIA_PER_REQUEST)) {
    form.append('file', part.blob, part.name);
    form.append('attachment_id', part.attachmentId);
  }
  form.append('source', source);
  let res: Response;
  try {
    res = await fetch(`/api/chat-media/${encodeURIComponent(conversationId)}`, {
      method: 'POST',
      body: form,
      signal,
    });
  } catch {
    return { kind: 'offline' };
  }
  if (res.ok) {
    try {
      const body = (await res.json()) as { items?: unknown };
      const items = Array.isArray(body.items)
        ? (body.items as StoredMediaItem[]).filter((item) =>
            isAttachmentId(item?.attachment_id),
          )
        : [];
      return { kind: 'stored', items };
    } catch {
      return { kind: 'failed', status: res.status };
    }
  }
  if (res.status === 400 || res.status === 413 || res.status === 415) {
    return { kind: 'refused', status: res.status };
  }
  if (res.status === 404) return { kind: 'not_found' };
  if (res.status === 401 || res.status === 403) return { kind: 'unauthenticated' };
  if (res.status === 507) return { kind: 'no_space' };
  // The proxy answers 502 for "the orchestrator could not be reached", which
  // is the server's side, not this browser being offline.
  return { kind: 'failed', status: res.status };
}

/* ------------------------------------------------------------ the backfill

   The server holds NO copy of a photo sent before 2026-10-02 — not in
   history, not in uploads, and V41's model-only copy expires after two hours.
   The one place such a photo still exists is the IndexedDB of the browser
   that sent it, and only until that browser logs out, clears site data or
   hits Safari's seven-day purge. So when this browser opens one of its own
   chats and still holds photos for turns that carry no `meta.images`, it
   uploads them (source=backfill) and writes the references, and from then on
   the photo shows on every device.

   The rules (CONTRACT §10), each for a reason:
   · deterministic ids, `bf-` + the first 32 hex of sha256(dataUrl): two tabs,
     two devices or a retry after a crash all mint the same id, and the
     server's first-write-wins makes every repeat a no-op;
   · single-flight per conversation, across tabs (Web Locks): two tabs
     uploading the same chat would race their history pushes;
   · at most 3 chats per idle tick, never on the render path: this is
     housekeeping and must not compete with the person's own work;
   · stops on 507 (the server is short of disk — more uploads only make it
     worse) and when offline;
   · ONE push per chat, re-applied once if a 409 adopted the server's copy
     over it (history stores meta verbatim; the server copy lacks the field);
   · only the viewer's own chats in the normal view. ChatApp is the only
     host, so a shared page or the admin transcript never runs it;
   · photos are taken only from a record written for THAT turn (same role,
     same words — idbCache's turnFingerprint). Records are keyed by index,
     and a thread replaced under them leaves one under an unrelated turn; a
     record older than the fingerprint is used only under a vision answer.

   It also REPAIRS (2026-10-02, QA): a turn sent with this build carries
   `meta.images` from the moment it is sent, but the server stores the bytes
   later, from the /chat body, and that store can be lost (a restart or a
   deploy mid-store, a failed /chat, a full disk). The reference then names
   nothing, and every other device shows "Image unavailable" for good. So when
   this browser holds the bytes for such a turn, it asks the server once per
   chat which ids it holds, and uploads the missing ones under the SAME ids
   (source=backfill; first write wins, so a store that did land is never
   overwritten). The reference is already right, so nothing is saved. */

/**
 * Photos this browser holds for one turn. A bare list is the host vouching
 * that the bytes ride on that very message; a record read back from
 * IndexedDB names the turn it was written for (`boundTo`), or nothing when it
 * predates that.
 */
export type HeldPhotos = string[] | HeldImageRecord;

/** A turn younger than this is not repaired: its /chat store may still be landing. */
export const REPAIR_GRACE_MS = 2 * 60_000;

/** The id a backfilled photo is stored under: `bf-<32 hex of sha256>`. */
export async function backfillAttachmentId(dataUrl: string): Promise<string | null> {
  // crypto.subtle exists only in a secure context. A plain-http LAN
  // deployment has none, and an id that is not deterministic would break the
  // idempotence the whole design leans on — so no id, no backfill.
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return null;
  try {
    const digest = await subtle.digest('SHA-256', new TextEncoder().encode(dataUrl));
    const hex = Array.from(new Uint8Array(digest).slice(0, 16), (b) =>
      b.toString(16).padStart(2, '0'),
    ).join('');
    return `bf-${hex}`;
  } catch {
    return null;
  }
}

const EXTENSION_BY_MIME: Record<string, string> = {
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/webp': 'webp',
  'image/gif': 'gif',
};

/** What the backfill needs from the app. ChatApp is the only implementation. */
export interface BackfillHost {
  /** The conversation's stored thread right now, or null when not cached. */
  messages(conversationId: string): ChatMessage[] | null;
  /**
   * The photos this browser still holds for the conversation, by message
   * index — the in-memory copy first, then IndexedDB (history.localImages).
   */
  localImages(conversationId: string): Promise<Map<number, HeldPhotos>>;
  /**
   * Persist an amended copy of the same thread through the normal store path
   * (one push), WITHOUT moving the chat in Recents, and fold it into the view.
   * Resolves once that push has settled.
   */
  save(conversationId: string, messages: ChatMessage[]): Promise<void>;
  /** False while a stream or a send owns this conversation's thread. */
  idle(conversationId: string): boolean;
}

export interface BackfillDeps {
  upload?: typeof uploadChatMedia;
  /** Which ids the server holds for a chat (the repair asks once per chat). */
  list?: typeof storedAttachmentIds;
  now?: () => number;
  attachmentId?: (dataUrl: string) => Promise<string | null>;
  /** Run `task` when the browser is idle. Tests run it at once. */
  schedule?: (task: () => void) => void;
  /** Web Locks, when the browser has them. */
  locks?: Pick<LockManager, 'request'> | null;
  online?: () => boolean;
}

/** At most this many conversations are worked on per idle tick. */
export const BACKFILL_CHATS_PER_TICK = 3;

/** One turn the backfill will upload for. */
interface BackfillTarget {
  index: number;
  content: string;
  dataUrls: string[];
  /**
   * A REPAIR: the turn's own `meta.images` ids, index for index with
   * `dataUrls`. Absent for a turn with no references yet.
   */
  ids?: string[];
}

/** The held bytes for a turn, and whether they are known to be that turn's. */
function heldFor(
  held: HeldPhotos,
  turn: ChatMessage,
): { urls: string[]; bound: 'own' | 'unknown' | 'other' } {
  if (Array.isArray(held)) return { urls: held, bound: 'own' };
  if (held.boundTo === undefined) return { urls: held.urls, bound: 'unknown' };
  return { urls: held.urls, bound: held.boundTo === turnFingerprint(turn) ? 'own' : 'other' };
}

/**
 * Do these bytes pair index for index with the turn's references? Only when
 * every entry is well formed (`imagesMetaFor` skipped none) and the counts and
 * every recorded type agree.
 */
function pairsWithRefs(turn: ChatMessage, urls: string[]): boolean {
  const raw = turn.meta?.images;
  const refs = storedImagesOf(turn);
  if (!Array.isArray(raw) || raw.length !== urls.length || refs.length !== urls.length) {
    return false;
  }
  return refs.every((ref, i) => !ref.mime || ref.mime === mimeFromDataUrl(urls[i]));
}

export type BackfillChatOutcome =
  /** Nothing to do, or done (possibly with some photos refused for good). */
  | 'done'
  /** Another tab holds this chat's lock, or this one is busy with a send. */
  | 'busy'
  /** Stop the chat; a later open may try again. */
  | 'later'
  /** Stop everything for this page's life (507, signed out). */
  | 'halt'
  /** Stop the queue until a chat is opened again. */
  | 'offline';

function idleScheduler(task: () => void): void {
  const ric = (globalThis as { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number })
    .requestIdleCallback;
  if (typeof ric === 'function') ric(task, { timeout: 10_000 });
  else setTimeout(task, 1_500);
}

function browserLocks(): Pick<LockManager, 'request'> | null {
  const nav = (globalThis as { navigator?: Navigator }).navigator;
  return nav?.locks && typeof nav.locks.request === 'function' ? nav.locks : null;
}

function browserOnline(): boolean {
  const nav = (globalThis as { navigator?: Navigator }).navigator;
  return nav?.onLine !== false;
}

/**
 * `messages` with each target turn's `meta.images` written, when that turn
 * is still the one the photos were read from (same place, still the person's,
 * same words) and still carries none. The SAME array when nothing changed.
 */
export function withBackfilledImages(
  messages: ChatMessage[],
  found: Map<number, { content: string; images: MessageImage[] }>,
): ChatMessage[] {
  let changed = false;
  const out = messages.map((m, i) => {
    const hit = found.get(i);
    if (!hit || m.role !== 'user' || m.content !== hit.content) return m;
    if (storedImagesOf(m).length > 0) return m;
    changed = true;
    return { ...m, meta: { ...(m.meta ?? {}), images: hit.images } };
  });
  return changed ? out : messages;
}

/**
 * The backfill: a queue of conversations, worked through a few per idle tick.
 * `request` is cheap and idempotent; call it whenever a chat is opened.
 */
export function createBackfill(host: BackfillHost, deps: BackfillDeps = {}) {
  const upload = deps.upload ?? uploadChatMedia;
  const list = deps.list ?? storedAttachmentIds;
  const now = deps.now ?? Date.now;
  const mintId = deps.attachmentId ?? backfillAttachmentId;
  const schedule = deps.schedule ?? idleScheduler;
  const locks = deps.locks === undefined ? browserLocks() : deps.locks;
  const online = deps.online ?? browserOnline;

  const queue: string[] = [];
  const running = new Set<string>();
  /** `<conversation>#<index>` turns whose photos the server refused for good. */
  const refused = new Set<string>();
  let halted = false;
  let booked = false;

  function book(): void {
    if (booked || halted || queue.length === 0) return;
    booked = true;
    schedule(() => {
      booked = false;
      void tick();
    });
  }

  async function tick(): Promise<void> {
    const batch = queue.splice(0, BACKFILL_CHATS_PER_TICK);
    for (const conversationId of batch) {
      if (halted) break;
      if (!online()) {
        queue.length = 0;
        return;
      }
      const outcome = await runExclusive(conversationId);
      if (outcome === 'halt') {
        halted = true;
        queue.length = 0;
        return;
      }
      if (outcome === 'offline') {
        queue.length = 0;
        return;
      }
    }
    book();
  }

  async function runExclusive(conversationId: string): Promise<BackfillChatOutcome> {
    if (running.has(conversationId)) return 'busy';
    running.add(conversationId);
    try {
      if (!locks) return await runChat(conversationId);
      // `ifAvailable`: a tab already backfilling this chat is doing exactly
      // this work; waiting for it would only repeat it.
      return await locks.request(
        `techsara-chat-media-backfill:${conversationId}`,
        { ifAvailable: true },
        async (lock) => (lock ? runChat(conversationId) : 'busy'),
      );
    } catch {
      return 'later';
    } finally {
      running.delete(conversationId);
    }
  }

  async function targetsOf(conversationId: string): Promise<BackfillTarget[]> {
    const messages = host.messages(conversationId);
    if (!messages?.length) return [];
    const local = await host.localImages(conversationId);
    const out: BackfillTarget[] = [];
    messages.forEach((m, index) => {
      if (m.role !== 'user') return;
      if (refused.has(`${conversationId}#${index}`)) return;
      const held = local.get(index);
      if (!held) return;
      const { urls: dataUrls, bound } = heldFor(held, m);
      if (dataUrls.length === 0 || dataUrls.length > MAX_MEDIA_PER_REQUEST) return;
      if (!dataUrls.every((url) => typeof url === 'string' && url.startsWith('data:'))) return;
      // Written for another turn that once sat at this index: never this one's.
      if (bound === 'other') return;
      const refs = storedImagesOf(m);
      if (refs.length === 0) {
        // A record from before records named their turn: trusted only where
        // the answer below it is about a picture.
        if (bound === 'unknown') {
          const next = messages[index + 1];
          if (next?.role !== 'assistant' || next.meta?.route !== 'vision') return;
        }
        out.push({ index, content: m.content, dataUrls });
        return;
      }
      // A repair writes these bytes under the turn's OWN ids, for good (first
      // write wins): only bytes known to be this turn's, paired one to one,
      // and not while the turn's own /chat store may still be landing.
      if (bound !== 'own' || !pairsWithRefs(m, dataUrls)) return;
      if (typeof m.createdAt === 'number' && now() - m.createdAt < REPAIR_GRACE_MS) return;
      out.push({ index, content: m.content, dataUrls, ids: refs.map((r) => r.attachment_id) });
    });
    return out;
  }

  /**
   * Upload the photos of a turn whose references the server does not hold,
   * under those same ids. Nothing to write back: the reference is the id.
   */
  async function repairTurn(
    conversationId: string,
    target: BackfillTarget & { ids: string[] },
    held: Set<string>,
  ): Promise<{ done: true } | { stop: BackfillChatOutcome } | { refused: true }> {
    const parts: MediaUploadPart[] = [];
    const seen = new Set<string>();
    target.ids.forEach((id, i) => {
      if (held.has(id) || seen.has(id)) return;
      seen.add(id);
      const blob = dataUrlToBlob(target.dataUrls[i]);
      if (!blob) return;
      const ext = EXTENSION_BY_MIME[blob.type] ?? 'img';
      parts.push({ attachmentId: id, blob, name: `image-${i + 1}.${ext}` });
    });
    if (parts.length === 0) return { done: true };
    const outcome = await upload(conversationId, parts, 'backfill');
    switch (outcome.kind) {
      case 'stored':
        return { done: true };
      case 'refused':
        return { refused: true };
      case 'no_space':
      case 'unauthenticated':
        return { stop: 'halt' };
      case 'offline':
        return { stop: 'offline' };
      case 'not_found':
      case 'failed':
        return { stop: 'later' };
    }
  }

  /** Upload one turn's photos. The images for meta, or what stopped it. */
  async function uploadTurn(
    conversationId: string,
    target: BackfillTarget,
  ): Promise<{ images: MessageImage[] } | { stop: BackfillChatOutcome } | { refused: true }> {
    const ids: string[] = [];
    for (const url of target.dataUrls) {
      const id = await mintId(url);
      if (!id) return { stop: 'halt' }; // no secure context: nothing can be done
      ids.push(id);
    }
    // One part per DISTINCT photo: the same picture twice in a turn is one
    // stored item, referenced twice.
    const parts: MediaUploadPart[] = [];
    const seen = new Set<string>();
    for (let i = 0; i < ids.length; i += 1) {
      if (seen.has(ids[i])) continue;
      seen.add(ids[i]);
      const blob = dataUrlToBlob(target.dataUrls[i]);
      if (!blob) return { refused: true };
      const ext = EXTENSION_BY_MIME[blob.type] ?? 'img';
      parts.push({ attachmentId: ids[i], blob, name: `image-${i + 1}.${ext}` });
    }
    const outcome = await upload(conversationId, parts, 'backfill');
    switch (outcome.kind) {
      case 'stored': {
        const byId = new Map(outcome.items.map((item) => [item.attachment_id, item]));
        const images: MessageImage[] = [];
        for (const id of ids) {
          const item = byId.get(id);
          if (!item) return { refused: true };
          const width = positiveInt(item.width);
          const height = positiveInt(item.height);
          images.push({
            attachment_id: id,
            ...(item.mime ? { mime: item.mime } : {}),
            ...(width && height ? { width, height } : {}),
          });
        }
        return { images };
      }
      case 'refused':
        return { refused: true };
      case 'no_space':
      case 'unauthenticated':
        return { stop: 'halt' };
      case 'offline':
        return { stop: 'offline' };
      case 'not_found':
      case 'failed':
        return { stop: 'later' };
    }
  }

  async function runChat(conversationId: string): Promise<BackfillChatOutcome> {
    if (!host.idle(conversationId)) return 'busy';
    const targets = await targetsOf(conversationId);
    if (targets.length === 0) return 'done';
    const found = new Map<number, { content: string; images: MessageImage[] }>();
    let stop: BackfillChatOutcome = 'done';
    // Asked once per chat, and only when there is something to repair.
    let held: Set<string> | null | undefined;
    for (const target of targets) {
      if (target.ids) {
        if (held === undefined) held = await list(conversationId);
        // Unknown is not "missing": try again at the next open.
        if (held === null) continue;
        const repaired = await repairTurn(conversationId, { ...target, ids: target.ids }, held);
        if ('refused' in repaired) refused.add(`${conversationId}#${target.index}`);
        if ('stop' in repaired) {
          stop = repaired.stop;
          break;
        }
        continue;
      }
      const result = await uploadTurn(conversationId, target);
      if ('images' in result) {
        found.set(target.index, { content: target.content, images: result.images });
        continue;
      }
      if ('refused' in result) {
        refused.add(`${conversationId}#${target.index}`);
        continue;
      }
      stop = result.stop;
      break;
    }
    // Whatever landed before a stop is written all the same: the bytes are on
    // the server now, and the reference is what makes them reachable.
    if (found.size > 0) await apply(conversationId, found);
    return stop;
  }

  /**
   * ONE save for the whole chat, then once more if the push that carried it
   * was refused (409) and the store adopted the server's copy — which does
   * not have the field — in its place.
   */
  async function apply(
    conversationId: string,
    found: Map<number, { content: string; images: MessageImage[] }>,
  ): Promise<void> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      // A send that started while the photos were uploading owns the thread
      // now; its own save would race this one. The next open finishes it.
      if (!host.idle(conversationId)) return;
      const latest = host.messages(conversationId);
      if (!latest) return;
      const next = withBackfilledImages(latest, found);
      if (next === latest) return;
      await host.save(conversationId, next);
    }
  }

  return {
    /** Queue a conversation; it is worked on at a later idle moment. */
    request(conversationId: string | null | undefined): void {
      if (halted || !conversationId) return;
      if (!CONVERSATION_ID.test(conversationId) || RESERVED_CONVERSATION.test(conversationId)) {
        return;
      }
      if (queue.includes(conversationId) || running.has(conversationId)) return;
      queue.push(conversationId);
      book();
    },
    /** Test seam: run one conversation now, skipping the queue. */
    runNow: (conversationId: string) => runExclusive(conversationId),
    /** For tests and diagnostics. */
    get halted() {
      return halted;
    },
  };
}

export type Backfill = ReturnType<typeof createBackfill>;
