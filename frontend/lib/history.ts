/**
 * Conversation history.
 *
 * V1 (§9) stored conversations in localStorage. V2 (§4b) swaps the
 * implementation to the orchestrator's /history API BEHIND THE SAME
 * INTERFACE: components still call the synchronous HistoryStore methods;
 * the local cache mirrors the server. Every write lands in the cache
 * immediately (instant UI) and is pushed to the server in the background;
 * failed pushes mark the conversation dirty and are retried on the next
 * refresh(). A one-time migration uploads pre-auth local conversations
 * after first login (§4b).
 *
 * V4 cache engine: the server store's cache is now an IN-MEMORY array
 * (synchronous, so the component-facing interface is unchanged) persisted
 * write-behind to IndexedDB with one record per conversation (lib/idbCache).
 * The old single-key localStorage blob hit the ~5 MiB Web Storage quota and
 * evicted whole conversations mid-chat with a toast per eviction; IndexedDB
 * has gigabyte-scale quota, and per-record writes avoid re-serializing every
 * conversation on every streamed token. The first boot after this change
 * imports the legacy blob and deletes it. Browsers without usable IndexedDB
 * fall back to the legacy blob persister, whose quota policy is unchanged:
 * on QuotaExceededError the OLDEST conversation (by updatedAt) is dropped
 * and the write retried, surfacing through onEvict for a toast. The v1
 * createHistoryStore (migration source, tests) still uses the blob directly.
 */

import type {
  ChatMessage,
  Conversation,
  ConversationSummary,
  Meta,
} from './types';
import {
  createIdbPersister,
  isIdbAvailable,
  type CachePersister,
} from './idbCache';
import {
  createHistoryApi,
  isConflict,
  isNotFound,
  isUnreachable,
  toEpoch,
  type ConversationPatch,
  type HistoryApi,
  type ServerConversationSummary,
  type ServerMessage,
} from './historyApi';
import {
  buildConversationExport,
  type ExportedConversation,
} from './exportMarkdown';

const STORAGE_KEY = 'techsara.history.v1';
const SYNC_KEY = 'techsara.history.sync.v1';
const TITLE_MAX = 40;

/** Minimal Storage surface — lets tests inject an in-memory fake. */
export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface HistoryStore {
  /** ACTIVE conversations only (V3: archived ones live in listArchived). */
  list(): ConversationSummary[];
  /** Archived conversations, newest first (V3 §2). */
  listArchived(): ConversationSummary[];
  get(id: string): Conversation | null;
  /** Creates a conversation titled from the first message (40-char cap). */
  create(firstMessage: string): Conversation;
  rename(id: string, title: string): void;
  remove(id: string): void;
  /** Replace a conversation's messages (meta included) and bump updatedAt. */
  saveMessages(id: string, messages: ChatMessage[]): void;
  /** V3 §2 — flag only; recency ordering is deliberately untouched. */
  setPinned(id: string, pinned: boolean): void;
  setArchived(id: string, archived: boolean): void;
}

/** V2 additions on top of the v1 interface (all backward-compatible). */
export interface ServerHistoryStore extends HistoryStore {
  /**
   * Resolves once the persistent cache (IndexedDB) has hydrated into memory
   * — await it before the first list()/get() on boot. Immediate for
   * synchronous engines (legacy blob, tests, SSR).
   */
  ready(): Promise<void>;
  /** Bind the store to the signed-in user; a USER CHANGE clears the cache. */
  setActiveUser(username: string): void;
  /** One-time upload of pre-auth local conversations (§4b). Returns count. */
  migrateLocalConversations(): Promise<number>;
  /** Pull server truth into the cache. false = offline/unauthorized. */
  refresh(): Promise<boolean>;
  /**
   * V3 §2: pull `?archived=true` into the cache — the sidebar's Archived
   * disclosure calls this the first time it is expanded. false = offline.
   */
  refreshArchived(): Promise<boolean>;
  /**
   * Ensure a conversation's messages are loaded (server fetch if stale).
   * `force` always refetches server truth — used when a detached generation
   * may have appended an assistant answer server-side (see lib/streams.ts).
   */
  load(id: string, opts?: { force?: boolean }): Promise<Conversation | null>;
  /**
   * V3 §2: the conversation as a downloadable Markdown file (loading its
   * messages first). null = the conversation is gone.
   */
  exportMarkdown(id: string): Promise<ExportedConversation | null>;
  /**
   * Discard every message after the first `keep`, locally and on the server.
   *
   * The ONLY sanctioned way to shorten a conversation. Call it exclusively
   * for a user-CONFIRMED regenerate of an older answer — the ordinary
   * saveMessages/sync path is physically unable to shrink a thread, which is
   * what stops a stale cache from destroying history. Rejects (throws) when
   * the server's thread is not the length the caller expects.
   */
  truncateMessages(id: string, keep: number): Promise<void>;
  /** Await all in-flight background pushes (used by tests). */
  flush(): Promise<void>;
}

export function titleFromFirstMessage(text: string): string {
  const oneLine = text.replace(/\s+/g, ' ').trim();
  if (oneLine.length === 0) return 'New chat';
  return oneLine.length <= TITLE_MAX
    ? oneLine
    : oneLine.slice(0, TITLE_MAX - 1).trimEnd() + '…';
}

export function newId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function isQuotaError(err: unknown): boolean {
  if (typeof DOMException !== 'undefined' && err instanceof DOMException) {
    return (
      err.name === 'QuotaExceededError' ||
      err.name === 'NS_ERROR_DOM_QUOTA_REACHED' ||
      err.code === 22
    );
  }
  return err instanceof Error && /quota/i.test(err.name + err.message);
}

/* ----------------------------------------------------------- local cache */

interface Cache {
  /** Resolves once hydration (and any legacy-blob import) finished. */
  ready: Promise<void>;
  readAll(): Conversation[];
  /**
   * Replace the cached set. `changed` names the conversation(s) this write
   * actually touched so per-record persisters only persist those; the legacy
   * blob cache ignores it and rewrites the whole blob.
   */
  writeAll(conversations: Conversation[], changed?: string | string[]): void;
  /** Await pending write-behind persistence (tests). */
  flushPersist(): Promise<void>;
  clear(): void;
}

function createCache(
  storage: StorageLike,
  onEvict?: (dropped: ConversationSummary) => void,
): Cache {
  return {
    ready: Promise.resolve(),

    flushPersist: () => Promise.resolve(),

    readAll() {
      try {
        const raw = storage.getItem(STORAGE_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        return Array.isArray(parsed) ? (parsed as Conversation[]) : [];
      } catch {
        return []; // corrupt payload: start clean rather than crash
      }
    },

    /** Write, dropping oldest conversations until the payload fits. */
    writeAll(conversations) {
      const current = [...conversations];
      for (;;) {
        try {
          storage.setItem(STORAGE_KEY, JSON.stringify(current));
          return;
        } catch (err) {
          if (!isQuotaError(err) || current.length === 0) throw err;
          let oldestIdx = 0;
          for (let i = 1; i < current.length; i++) {
            if (current[i].updatedAt < current[oldestIdx].updatedAt) {
              oldestIdx = i;
            }
          }
          const [dropped] = current.splice(oldestIdx, 1);
          onEvict?.({
            id: dropped.id,
            title: dropped.title,
            createdAt: dropped.createdAt,
            updatedAt: dropped.updatedAt,
          });
        }
      }
    },

    clear() {
      try {
        storage.removeItem(STORAGE_KEY);
      } catch {
        // storage unavailable — nothing to clear
      }
    },
  };
}

/**
 * The legacy single-blob localStorage cache as a CachePersister — the
 * fallback when IndexedDB is unavailable (some private modes) and the
 * synchronous engine under the v1 store. Keeps the old on-disk format,
 * including the quota-evict loop.
 */
function createBlobPersister(
  storage: StorageLike,
  onEvict?: (dropped: ConversationSummary) => void,
): CachePersister {
  const blob = createCache(storage, onEvict);
  return {
    mode: 'sync',
    loadAll: () => blob.readAll(),
    put(conversations) {
      const all = blob.readAll();
      for (const conv of conversations) {
        const idx = all.findIndex((c) => c.id === conv.id);
        if (idx === -1) all.push(conv);
        else all[idx] = conv;
      }
      blob.writeAll(all);
    },
    remove(ids) {
      const drop = new Set(ids);
      blob.writeAll(blob.readAll().filter((c) => !drop.has(c.id)));
    },
    clear: () => blob.clear(),
  };
}

/** How long streamed-token writes coalesce before an async persist. */
const PERSIST_DEBOUNCE_MS = 300;

/**
 * Synchronous in-memory cache persisted write-behind through a
 * CachePersister. Reads/writes are instant (components keep their sync
 * interface); async persisters (IndexedDB) get debounced per-conversation
 * writes, sync persisters (legacy blob, tests) are written through
 * immediately so existing call-order expectations hold.
 */
function createMemoryCache(
  persister: CachePersister,
  legacy?: { read(): Conversation[]; discard(): void },
): Cache {
  let conversations: Conversation[] = [];
  const pendingPuts = new Set<string>();
  const pendingRemoves = new Set<string>();
  let timer: ReturnType<typeof setTimeout> | null = null;
  let inFlight: Promise<void> = Promise.resolve();

  function flushNow(): void {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    if (pendingPuts.size === 0 && pendingRemoves.size === 0) return;
    const byId = new Map(conversations.map((c) => [c.id, c]));
    const puts = [...pendingPuts]
      .map((id) => byId.get(id))
      .filter((c): c is Conversation => c !== undefined);
    const removes = [...pendingRemoves];
    pendingPuts.clear();
    pendingRemoves.clear();
    const work = (async () => {
      if (puts.length > 0) await persister.put(puts);
      if (removes.length > 0) await persister.remove(removes);
    })().catch(() => {
      // Persistence is best-effort; the server holds the durable copy.
    });
    inFlight = inFlight.then(() => work);
  }

  function schedule(): void {
    if (persister.mode === 'sync') {
      flushNow();
      return;
    }
    if (!timer) timer = setTimeout(flushNow, PERSIST_DEBOUNCE_MS);
  }

  let ready: Promise<void>;
  if (persister.mode === 'sync') {
    // Synchronous engines (legacy blob, tests) hydrate before the store is
    // handed out, so list()/get() right after construction see the data.
    try {
      conversations = persister.loadAll() as Conversation[];
    } catch {
      conversations = [];
    }
    ready = Promise.resolve();
  } else {
    ready = (async () => {
      let loaded: Conversation[] = [];
      try {
        loaded = await persister.loadAll();
      } catch {
        // unavailable — run memory-only; the server refresh() repopulates
      }
      // Import the pre-IndexedDB localStorage blob exactly once.
      if (legacy && loaded.length === 0) {
        const old = legacy.read();
        if (old.length > 0) {
          loaded = old;
          try {
            await persister.put(old);
            legacy.discard();
          } catch {
            // keep the blob so the next boot can retry the import
          }
        }
      }
      // Merge under anything created while hydration was in flight —
      // in-memory (newer) wins on id collisions.
      const have = new Set(conversations.map((c) => c.id));
      conversations = [
        ...loaded.filter((c) => !have.has(c.id)),
        ...conversations,
      ];
    })();
  }

  return {
    ready,

    readAll: () => conversations,

    writeAll(next, changed) {
      const prevIds = new Set(conversations.map((c) => c.id));
      conversations = next;
      const nextIds = new Set(next.map((c) => c.id));
      const touched =
        changed === undefined
          ? [...nextIds]
          : Array.isArray(changed)
            ? changed
            : [changed];
      for (const id of touched) {
        if (nextIds.has(id)) {
          pendingPuts.add(id);
          pendingRemoves.delete(id);
        } else if (prevIds.has(id) || pendingPuts.has(id)) {
          pendingPuts.delete(id);
          pendingRemoves.add(id);
        }
      }
      schedule();
    },

    async flushPersist() {
      flushNow();
      await inFlight;
    },

    clear() {
      conversations = [];
      pendingPuts.clear();
      pendingRemoves.clear();
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      void Promise.resolve(persister.clear()).catch(() => {
        // best-effort
      });
    },
  };
}

/**
 * Summaries for the conversations matching `wantArchived`, in sidebar order:
 * pinned first (V3 §2), then newest first. Same-millisecond ties break by
 * insertion order (later in the cache = created/touched later) so ordering
 * is deterministic even for rapid successive writes.
 */
function summarize(
  conversations: Conversation[],
  wantArchived: boolean,
): ConversationSummary[] {
  return conversations
    .map((conversation, index) => ({ conversation, index }))
    .filter(({ conversation }) => (conversation.archived === true) === wantArchived)
    .sort(
      (a, b) =>
        Number(b.conversation.pinned === true) -
          Number(a.conversation.pinned === true) ||
        b.conversation.updatedAt - a.conversation.updatedAt ||
        b.index - a.index,
    )
    .map(({ conversation }) => ({
      id: conversation.id,
      title: conversation.title,
      createdAt: conversation.createdAt,
      updatedAt: conversation.updatedAt,
      pinned: conversation.pinned === true,
      archived: conversation.archived === true,
    }));
}

function storeOverCache(cache: Cache): HistoryStore {
  /** Set a flag without touching updatedAt (V3 §1: ordering is unchanged). */
  function setFlag(id: string, patch: Pick<Conversation, 'pinned' | 'archived'>) {
    const all = cache.readAll();
    const target = all.find((c) => c.id === id);
    if (!target) return;
    Object.assign(target, patch);
    cache.writeAll(all, id);
  }

  return {
    list() {
      return summarize(cache.readAll(), false);
    },

    listArchived() {
      return summarize(cache.readAll(), true);
    },

    get(id) {
      return cache.readAll().find((c) => c.id === id) ?? null;
    },

    create(firstMessage) {
      const now = Date.now();
      const conversation: Conversation = {
        id: newId(),
        title: titleFromFirstMessage(firstMessage),
        createdAt: now,
        updatedAt: now,
        messages: [],
      };
      cache.writeAll([...cache.readAll(), conversation], conversation.id);
      return conversation;
    },

    rename(id, title) {
      const trimmed = title.trim();
      if (!trimmed) return;
      const all = cache.readAll();
      const target = all.find((c) => c.id === id);
      if (!target) return;
      target.title = trimmed.slice(0, TITLE_MAX);
      target.updatedAt = Date.now();
      cache.writeAll(all, id);
    },

    remove(id) {
      cache.writeAll(
        cache.readAll().filter((c) => c.id !== id),
        id,
      );
    },

    saveMessages(id, messages) {
      const all = cache.readAll();
      const target = all.find((c) => c.id === id);
      if (!target) return;
      target.messages = messages;
      target.updatedAt = Date.now();
      cache.writeAll(all, id);
    },

    setPinned(id, pinned) {
      setFlag(id, { pinned });
    },

    setArchived(id, archived) {
      setFlag(id, { archived });
    },
  };
}

/**
 * The v1 local-only store — still the cache layer under the server store,
 * and the storage engine for pre-login (migration source) data.
 */
export function createHistoryStore(
  storage: StorageLike,
  onEvict?: (dropped: ConversationSummary) => void,
): HistoryStore {
  return storeOverCache(createCache(storage, onEvict));
}

/* ------------------------------------------------------- server sync */

interface SyncState {
  /** The user this cache belongs to; a change wipes the cache. */
  username?: string;
  /** One-time §4b migration completed. */
  migrated?: boolean;
  /**
   * Per conversation: the exact message ids known to be on the server (in
   * order). 'unknown' = the conversation exists server-side but its messages
   * were never fetched. Absent = never pushed.
   */
  pushed: Record<string, string[] | 'unknown'>;
  /** Conversations whose last push failed — retried on refresh(). */
  dirty: string[];
  /** Deletes that have not reached the server yet. */
  deleted: string[];
}

function sameIds(a: string[], msgs: ChatMessage[]): boolean {
  return a.length === msgs.length && a.every((id, i) => msgs[i].id === id);
}

function isPrefix(prefix: string[], msgs: ChatMessage[]): boolean {
  return (
    prefix.length <= msgs.length &&
    prefix.every((id, i) => msgs[i].id === id)
  );
}

export interface ServerHistoryStoreOptions {
  storage: StorageLike;
  api?: HistoryApi;
  onEvict?: (dropped: ConversationSummary) => void;
  /**
   * Durable backing for the in-memory cache. Defaults to IndexedDB when the
   * browser has it (with the legacy localStorage blob as fallback and
   * one-time migration source), else the legacy blob alone — which keeps
   * tests and non-IDB environments on the old synchronous behavior.
   */
  persister?: CachePersister;
}

export function createServerHistoryStore(
  options: ServerHistoryStoreOptions,
): ServerHistoryStore {
  const blobPersister = createBlobPersister(options.storage, options.onEvict);
  const useIdb = options.persister === undefined && isIdbAvailable();
  const persister =
    options.persister ??
    (useIdb ? createIdbPersister(blobPersister) : blobPersister);
  // When IndexedDB is the engine, the old blob (if any) is imported once and
  // deleted; while the blob IS the engine it must not be treated as legacy.
  const legacy =
    persister.mode === 'async'
      ? {
          read: () => createCache(options.storage).readAll(),
          discard: () => {
            try {
              options.storage.removeItem(STORAGE_KEY);
            } catch {
              // best-effort
            }
          },
        }
      : undefined;
  const cache = createMemoryCache(persister, legacy);
  const local = storeOverCache(cache);
  const api = options.api ?? createHistoryApi();

  /** Per-conversation push chains keep background syncs ordered. */
  const chains = new Map<string, Promise<void>>();
  const inFlight = new Set<Promise<void>>();

  function readSync(): SyncState {
    try {
      const raw = options.storage.getItem(SYNC_KEY);
      const parsed = raw ? (JSON.parse(raw) as Partial<SyncState>) : {};
      return {
        username:
          typeof parsed.username === 'string' ? parsed.username : undefined,
        migrated: parsed.migrated === true,
        pushed:
          parsed.pushed && typeof parsed.pushed === 'object'
            ? (parsed.pushed as SyncState['pushed'])
            : {},
        dirty: Array.isArray(parsed.dirty) ? parsed.dirty : [],
        deleted: Array.isArray(parsed.deleted) ? parsed.deleted : [],
      };
    } catch {
      return { pushed: {}, dirty: [], deleted: [] };
    }
  }

  function mutateSync(fn: (s: SyncState) => void): void {
    const s = readSync();
    fn(s);
    try {
      options.storage.setItem(SYNC_KEY, JSON.stringify(s));
    } catch {
      // sync bookkeeping is best-effort; worst case we re-push a conversation
    }
  }

  function markDirty(id: string): void {
    mutateSync((s) => {
      if (!s.dirty.includes(id)) s.dirty.push(id);
    });
  }

  function upsertCached(conv: Conversation): void {
    const all = cache.readAll();
    const idx = all.findIndex((c) => c.id === conv.id);
    if (idx === -1) all.push(conv);
    else all[idx] = conv;
    cache.writeAll(all, conv.id);
  }

  function toServerMessage(m: ChatMessage): ServerMessage {
    return { role: m.role, content: m.content, meta: m.meta ?? null };
  }

  function flagsOf(conv: Conversation): ConversationPatch {
    return { pinned: conv.pinned === true, archived: conv.archived === true };
  }

  /**
   * Fold server rows into the cache: unknown ids are added (messages stay
   * lazy), known ones adopt the server's title / updated_at / V3 flags
   * unless a local push for them is still pending.
   */
  function mergeServerRows(rows: ServerConversationSummary[]): void {
    const all = cache.readAll();
    const changed: string[] = [];
    for (const sc of rows) {
      const idx = all.findIndex((c) => c.id === sc.id);
      if (idx === -1) {
        const updatedAt = toEpoch(sc.updated_at, Date.now());
        all.push({
          id: sc.id,
          title: sc.title || 'Conversation',
          createdAt: toEpoch(sc.created_at, updatedAt),
          updatedAt,
          pinned: sc.pinned === true || sc.pinned === 1,
          archived: sc.archived === true || sc.archived === 1,
          messages: [], // fetched lazily by load()
        });
        mutateSync((s) => {
          s.pushed[sc.id] = 'unknown';
        });
        changed.push(sc.id);
      } else if (!readSync().dirty.includes(sc.id)) {
        const conv = all[idx];
        let touched = false;
        if (sc.title && conv.title !== sc.title) {
          conv.title = sc.title;
          touched = true;
        }
        const serverUpdated = toEpoch(sc.updated_at, conv.updatedAt);
        if (serverUpdated > conv.updatedAt) {
          conv.updatedAt = serverUpdated;
          touched = true;
        }
        for (const flag of ['pinned', 'archived'] as const) {
          const value = sc[flag];
          if (value === undefined || value === null) continue;
          const next = value === true || value === 1;
          if (conv[flag] !== next) {
            conv[flag] = next;
            touched = true;
          }
        }
        if (touched) changed.push(sc.id);
      }
    }
    if (changed.length > 0) cache.writeAll(all, changed);
  }

  /**
   * Make the server match the local copy.
   *
   * This used to DELETE the conversation and recreate it from the local
   * messages. That destroyed entire threads whenever the local copy was
   * empty or stale — a chat the server listed but this browser had never
   * opened (cached as `messages: []`), or one dropped by a quota eviction —
   * because the "rebuild" then wrote back a single message and the server's
   * cascade removed the rest.
   *
   * Now it upserts and asks the server to REPLACE the thread atomically; the
   * server refuses (409) any replace that would reduce the message count, and
   * we recover by adopting server truth instead of overwriting it.
   */
  async function pushAll(conv: Conversation): Promise<void> {
    try {
      await api.create(conv.id, conv.title);
    } catch (err) {
      // 409 = it already exists, which is exactly what we want here.
      if (!isConflict(err)) throw err;
    }
    try {
      await api.replaceMessages(
        conv.id,
        conv.messages.map(toServerMessage),
      );
    } catch (err) {
      if (isConflict(err)) {
        // The server holds MORE than we do: our copy is stale, not canonical.
        // Pull its version down rather than destroying it.
        await loadConversation(conv.id, true);
        mutateSync((s) => {
          s.dirty = s.dirty.filter((d) => d !== conv.id);
        });
        return;
      }
      throw err;
    }
    // Flags are not part of the message sync; re-apply what we carry locally.
    if (conv.pinned || conv.archived) {
      await api.update(conv.id, flagsOf(conv));
    }
    mutateSync((s) => {
      s.pushed[conv.id] = conv.messages.map((m) => m.id);
      s.dirty = s.dirty.filter((d) => d !== conv.id);
    });
  }

  /** Bring the server copy of one conversation up to date with the cache. */
  async function syncConversation(id: string): Promise<void> {
    const conv = local.get(id);
    if (!conv) {
      // Deleted locally while a push was pending — nothing left to sync.
      mutateSync((s) => {
        s.dirty = s.dirty.filter((d) => d !== id);
      });
      return;
    }
    const pushed = readSync().pushed[id];
    if (Array.isArray(pushed) && isPrefix(pushed, conv.messages)) {
      if (pushed.length === conv.messages.length) return; // already in sync
      for (const m of conv.messages.slice(pushed.length)) {
        await api.appendMessage(id, toServerMessage(m));
      }
      mutateSync((s) => {
        s.pushed[id] = conv.messages.map((m) => m.id);
        s.dirty = s.dirty.filter((d) => d !== id);
      });
      return;
    }
    // Never pushed, unknown server contents, or a diverged tail
    // (regenerate) — rebuild the server copy from the local one.
    await pushAll(conv);
  }

  /**
   * PUT a subset of {title, pinned, archived}. A 404 means the conversation
   * never reached the server — push it whole (pushAll carries the flags).
   */
  async function pushPatch(id: string, patch: ConversationPatch): Promise<void> {
    try {
      await api.update(id, patch);
    } catch (err) {
      if (!isNotFound(err)) throw err;
      const conv = local.get(id);
      if (conv) await pushAll(conv);
    }
  }

  /** Ensure a conversation's messages are in the cache (server fetch if stale). */
  async function loadConversation(
    id: string,
    force = false,
  ): Promise<Conversation | null> {
    const cached = local.get(id);
    const s = readSync();
    const pushed = s.pushed[id];
    if (
      !force &&
      cached &&
      Array.isArray(pushed) &&
      sameIds(pushed, cached.messages)
    ) {
      return cached; // cache is exactly what the server has
    }
    if (!force && cached && (pushed === undefined || s.dirty.includes(id))) {
      return cached; // local copy is ahead; background sync will push it
    }
    try {
      const server = await api.get(id);
      if (force && cached && server.messages.length < cached.messages.length) {
        return cached; // server is behind the local copy — keep local truth
      }
      const now = Date.now();
      const messages: ChatMessage[] = server.messages.map((m, i) => ({
        id: `srv-${id}-${i}`,
        role: m.role === 'user' ? 'user' : 'assistant',
        content: typeof m.content === 'string' ? m.content : '',
        ...(m.meta ? { meta: m.meta as Meta } : {}),
        ...(m.role === 'user' ? {} : { status: 'done' as const }),
        createdAt: now - (server.messages.length - i),
      }));
      const conv: Conversation = {
        id,
        title: server.title || cached?.title || 'Conversation',
        createdAt: cached?.createdAt ?? now,
        updatedAt: cached?.updatedAt ?? now,
        ...(cached?.pinned !== undefined ? { pinned: cached.pinned } : {}),
        ...(cached?.archived !== undefined ? { archived: cached.archived } : {}),
        messages,
      };
      upsertCached(conv);
      mutateSync((st) => {
        st.pushed[id] = messages.map((m) => m.id);
      });
      return conv;
    } catch {
      return cached; // offline — serve the cached copy (may be stale)
    }
  }

  function enqueue(id: string, task: () => Promise<void>): void {
    const prev = chains.get(id) ?? Promise.resolve();
    const next = prev.then(task).catch(() => markDirty(id));
    chains.set(id, next);
    inFlight.add(next);
    void next.finally(() => inFlight.delete(next));
  }

  return {
    /* -------- v1 interface: synchronous over the cache, pushed behind */

    ready: () => cache.ready,
    list: () => local.list(),
    listArchived: () => local.listArchived(),
    get: (id) => local.get(id),

    create(firstMessage) {
      const conv = local.create(firstMessage);
      enqueue(conv.id, () => syncConversation(conv.id));
      return conv;
    },

    rename(id, title) {
      local.rename(id, title);
      const applied = local.get(id)?.title;
      if (!applied) return;
      enqueue(id, () => pushPatch(id, { title: applied }));
    },

    remove(id) {
      local.remove(id);
      // If the conversation was never pushed to the server there is nothing to
      // DELETE there — skip the call so it doesn't 404 in the network log.
      const wasSynced = readSync().pushed[id] !== undefined;
      mutateSync((s) => {
        delete s.pushed[id];
        s.dirty = s.dirty.filter((d) => d !== id);
        if (wasSynced && !s.deleted.includes(id)) s.deleted.push(id);
      });
      if (!wasSynced) return;
      enqueue(id, async () => {
        try {
          await api.remove(id);
        } catch (err) {
          if (!isNotFound(err)) throw err;
        }
        mutateSync((s) => {
          s.deleted = s.deleted.filter((d) => d !== id);
        });
      });
    },

    saveMessages(id, messages) {
      local.saveMessages(id, messages);
      enqueue(id, () => syncConversation(id));
    },

    /* ------------------------------------------------ v3 additions */

    async truncateMessages(id, keep) {
      const conv = local.get(id);
      if (!conv) return;
      const before = conv.messages.length;
      if (keep >= before) return;
      // Server FIRST: if it refuses (someone else appended, or the row is
      // gone) the local copy must stay intact rather than losing turns that
      // still exist server-side.
      await api.truncateMessages(id, keep, before);
      const kept = conv.messages.slice(0, keep);
      local.saveMessages(id, kept);
      mutateSync((s) => {
        s.pushed[id] = kept.map((m) => m.id);
        s.dirty = s.dirty.filter((d) => d !== id);
      });
    },

    setPinned(id, pinned) {
      local.setPinned(id, pinned);
      if (!local.get(id)) return;
      enqueue(id, () => pushPatch(id, { pinned }));
    },

    setArchived(id, archived) {
      local.setArchived(id, archived);
      if (!local.get(id)) return;
      enqueue(id, () => pushPatch(id, { archived }));
    },

    async exportMarkdown(id) {
      const conv = await loadConversation(id);
      return conv ? buildConversationExport(conv) : null;
    },

    /* ------------------------------------------------ v2 additions */

    setActiveUser(username) {
      const s = readSync();
      if (s.username === username) return;
      if (s.username && s.username !== username) {
        // Different account on this browser: never show (or upload)
        // another user's conversations.
        cache.clear();
        try {
          options.storage.setItem(
            SYNC_KEY,
            JSON.stringify({
              username,
              migrated: true, // nothing local left to migrate
              pushed: {},
              dirty: [],
              deleted: [],
            } satisfies SyncState),
          );
        } catch {
          // best-effort
        }
        return;
      }
      mutateSync((st) => {
        st.username = username;
      });
    },

    async migrateLocalConversations() {
      if (readSync().migrated) return 0;
      let count = 0;
      for (const conv of cache.readAll()) {
        if (readSync().pushed[conv.id] !== undefined) continue; // already up
        if (conv.messages.length === 0) continue;
        await syncConversation(conv.id);
        count += 1;
      }
      mutateSync((s) => {
        s.migrated = true;
      });
      return count;
    },

    async refresh() {
      try {
        // 1. Finish deletes that never reached the server.
        for (const id of readSync().deleted) {
          try {
            await api.remove(id);
          } catch (err) {
            if (!isNotFound(err)) throw err;
          }
          mutateSync((s) => {
            s.deleted = s.deleted.filter((d) => d !== id);
          });
        }

        // 2. Re-push conversations whose last background push failed. Flags
        //    are not part of the message sync, so re-apply them explicitly.
        for (const id of [...readSync().dirty]) {
          await syncConversation(id);
          const conv = local.get(id);
          if (conv) await api.update(id, flagsOf(conv));
        }

        // 3. Pull BOTH server lists (V3 §1: archived chats are hidden from
        //    the default one) and reconcile the cache with them.
        const activeList = await api.list();
        let archivedList: ServerConversationSummary[] = [];
        let archivedKnown = false;
        try {
          archivedList = await api.list({ archived: true });
          archivedKnown = true;
        } catch (err) {
          // A network failure means the whole refresh failed; a rejection
          // from a pre-V3 backend just means "no archived rows to reconcile"
          // — and locally archived conversations must survive that.
          if (isUnreachable(err)) throw err;
        }
        const serverList = [...activeList, ...archivedList];
        const serverIds = new Set(serverList.map((c) => c.id));

        for (const summary of [...local.list(), ...local.listArchived()]) {
          if (serverIds.has(summary.id)) continue;
          if (summary.archived && !archivedKnown) continue;
          if (readSync().pushed[summary.id] !== undefined) {
            // Was on the server and is gone now — deleted elsewhere.
            local.remove(summary.id);
            mutateSync((s) => {
              delete s.pushed[summary.id];
            });
          } else {
            // Local-only (created offline) — push it now.
            await syncConversation(summary.id);
          }
        }

        mergeServerRows(serverList);
        return true;
      } catch {
        return false; // offline or unauthorized — the cache keeps working
      }
    },

    async refreshArchived() {
      try {
        mergeServerRows(await api.list({ archived: true }));
        return true;
      } catch {
        return false;
      }
    },

    load: (id, opts) => loadConversation(id, opts?.force === true),

    async flush() {
      while (inFlight.size > 0) {
        await Promise.all([...inFlight]);
      }
      await cache.flushPersist();
    },
  };
}

/* ------------------------------------------------- browser singleton */

let browserStore: ServerHistoryStore | null = null;
let evictListener: ((dropped: ConversationSummary) => void) | undefined;

export function setEvictListener(
  fn: (dropped: ConversationSummary) => void,
): void {
  evictListener = fn;
}

const NOOP_STORAGE: StorageLike = {
  getItem: () => null,
  setItem: () => undefined,
  removeItem: () => undefined,
};

export function getHistoryStore(): ServerHistoryStore {
  if (typeof window === 'undefined') {
    // SSR-safe no-op store; real reads happen client-side only.
    return createServerHistoryStore({
      storage: NOOP_STORAGE,
      api: {
        list: async () => [],
        get: async () => ({ id: '', title: '', messages: [] }),
        create: async () => undefined,
        update: async () => undefined,
        remove: async () => undefined,
        appendMessage: async () => undefined,
        replaceMessages: async () => undefined,
        truncateMessages: async () => undefined,
      },
    });
  }
  if (!browserStore) {
    browserStore = createServerHistoryStore({
      storage: window.localStorage,
      onEvict: (dropped) => evictListener?.(dropped),
    });
  }
  return browserStore;
}
