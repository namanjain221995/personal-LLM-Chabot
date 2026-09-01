/**
 * IndexedDB persister for the conversation cache.
 *
 * Replaces the single-key localStorage blob that hit the ~5 MiB Web Storage
 * quota and evicted whole conversations mid-chat. IndexedDB shares the
 * origin's gigabyte-scale quota pool (Chromium: up to 60% of disk; Firefox:
 * min(10% of disk, 10 GiB); Safari 17+: ~60% of disk), so quota pressure
 * effectively disappears — but the cache must STAY a rebuildable mirror of
 * the server: Safari's ITP deletes all script-writable storage (IndexedDB
 * included) after 7 days of Safari use without visiting the site, and other
 * browsers LRU-evict origins under disk pressure.
 *
 * Layout follows the per-record rule (whole-state single-record writes block
 * the main thread on the synchronous structured clone and can crash the
 * tab — web.dev): one record per conversation in `conversations`, and image
 * data-URLs split into write-once records in `images` keyed `<convId>#<msgIdx>`
 * so re-persisting a streaming conversation never re-clones multi-MB base64
 * strings. Messages only append or truncate from the tail, so a message's
 * index is a stable image key.
 *
 * Every method fails soft: on the first IndexedDB error the persister flips
 * to `broken` and delegates to the caller-supplied fallback (the legacy
 * localStorage blob) so private-mode browsers keep working.
 */

import type { ChatMessage, Conversation } from './types';

const DB_NAME = 'techsara-history';
const DB_VERSION = 1;
const CONV_STORE = 'conversations';
const IMAGE_STORE = 'images';

/** Durable backing for the in-memory conversation cache. */
export interface CachePersister {
  /**
   * 'sync' persisters hydrate before the constructor returns (localStorage,
   * tests); 'async' ones resolve `loadAll` later and get debounced writes.
   */
  mode: 'sync' | 'async';
  loadAll(): Conversation[] | Promise<Conversation[]>;
  /** Persist the given (changed) conversations. */
  put(conversations: Conversation[]): void | Promise<void>;
  remove(ids: string[]): void | Promise<void>;
  clear(): void | Promise<void>;
}

interface ImageRecord {
  /** `<conversationId>#<messageIndex>` — ids never contain '#'. */
  key: string;
  convId: string;
  single?: string;
  multi?: string[];
}

function hasImages(m: ChatMessage): boolean {
  return (
    typeof m.imageDataUrl === 'string' ||
    (Array.isArray(m.imageDataUrls) && m.imageDataUrls.length > 0)
  );
}

function stripImages(m: ChatMessage): ChatMessage {
  if (!hasImages(m)) return m;
  const rest = { ...m };
  delete rest.imageDataUrl;
  delete rest.imageDataUrls;
  return rest;
}

function requestDone<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function txDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error ?? new Error('transaction aborted'));
  });
}

function imageRange(convId: string): IDBKeyRange {
  return IDBKeyRange.bound(`${convId}#`, `${convId}#￿`);
}

export function isIdbAvailable(): boolean {
  return typeof indexedDB !== 'undefined';
}

/**
 * The database for one signed-in account (enterprise auth retrofit): every
 * user gets their OWN database, `techsara-history:u<id>`, so an account
 * switch never reads another account's cache — isolation by name, with the
 * wipe on switch/logout as the belt to this suspender. `userKey` is the
 * stable scoping key the history store binds (`u<id>`), never a display name.
 */
export function userDbName(userKey: string): string {
  return `${DB_NAME}:${userKey}`;
}

/**
 * Best-effort removal of the LEGACY shared database (the pre-auth,
 * origin-wide 'techsara-history'). Called on account change and logout so a
 * cache written before per-user databases existed cannot outlive the account
 * that wrote it. Fire-and-forget by design: deletion of a database another
 * tab still holds open completes when that connection closes.
 */
export function deleteLegacyDb(): void {
  if (!isIdbAvailable()) return;
  try {
    indexedDB.deleteDatabase(DB_NAME);
  } catch {
    // best-effort — the per-user store never reads this database anyway
  }
}

/**
 * IndexedDB persister with automatic fallback. `migrateLegacy` is called once
 * after a successful open when the database is empty — it returns whatever
 * conversations the old localStorage blob held, which are then imported and
 * the blob deleted by the caller. `dbName` selects the per-user database
 * (userDbName); omitted, it opens the legacy shared one.
 */
export function createIdbPersister(
  fallback: CachePersister,
  onBroken?: (err: unknown) => void,
  dbName: string = DB_NAME,
): CachePersister {
  let broken = false;
  let dbPromise: Promise<IDBDatabase> | null = null;

  function fail(err: unknown): void {
    if (!broken) {
      broken = true;
      onBroken?.(err);
    }
  }

  function openDb(): Promise<IDBDatabase> {
    if (!dbPromise) {
      dbPromise = new Promise((resolve, reject) => {
        const req = indexedDB.open(dbName, DB_VERSION);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains(CONV_STORE)) {
            db.createObjectStore(CONV_STORE, { keyPath: 'id' });
          }
          if (!db.objectStoreNames.contains(IMAGE_STORE)) {
            db.createObjectStore(IMAGE_STORE, { keyPath: 'key' });
          }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
        req.onblocked = () => reject(new Error('indexeddb open blocked'));
      });
    }
    return dbPromise;
  }

  return {
    mode: 'async',

    async loadAll() {
      if (broken) return fallback.loadAll();
      try {
        const db = await openDb();
        const tx = db.transaction([CONV_STORE, IMAGE_STORE], 'readonly');
        const conversations = requestDone(
          tx.objectStore(CONV_STORE).getAll() as IDBRequest<Conversation[]>,
        );
        const images = requestDone(
          tx.objectStore(IMAGE_STORE).getAll() as IDBRequest<ImageRecord[]>,
        );
        const [convs, imgs] = await Promise.all([conversations, images]);
        if (imgs.length > 0) {
          const byConv = new Map<string, Map<number, ImageRecord>>();
          for (const rec of imgs) {
            const idx = Number(rec.key.slice(rec.convId.length + 1));
            if (!Number.isInteger(idx)) continue;
            let forConv = byConv.get(rec.convId);
            if (!forConv) byConv.set(rec.convId, (forConv = new Map()));
            forConv.set(idx, rec);
          }
          for (const conv of convs) {
            const forConv = byConv.get(conv.id);
            if (!forConv) continue;
            conv.messages = conv.messages.map((m, i) => {
              const rec = forConv.get(i);
              if (!rec) return m;
              return {
                ...m,
                ...(rec.single !== undefined ? { imageDataUrl: rec.single } : {}),
                ...(rec.multi !== undefined ? { imageDataUrls: rec.multi } : {}),
              };
            });
          }
        }
        return convs;
      } catch (err) {
        fail(err);
        return fallback.loadAll();
      }
    },

    async put(conversations) {
      if (broken) return fallback.put(conversations);
      try {
        const db = await openDb();
        const tx = db.transaction([CONV_STORE, IMAGE_STORE], 'readwrite');
        const convStore = tx.objectStore(CONV_STORE);
        const imgStore = tx.objectStore(IMAGE_STORE);
        for (const conv of conversations) {
          convStore.put({
            ...conv,
            messages: conv.messages.map(stripImages),
          });
          // Write-once image records: add the missing, drop the truncated.
          const existing = new Set(
            (await requestDone(
              imgStore.getAllKeys(imageRange(conv.id)) as IDBRequest<string[]>,
            )) as string[],
          );
          conv.messages.forEach((m, i) => {
            const key = `${conv.id}#${i}`;
            if (!hasImages(m) || existing.has(key)) return;
            const rec: ImageRecord = { key, convId: conv.id };
            if (m.imageDataUrl !== undefined) rec.single = m.imageDataUrl;
            if (m.imageDataUrls !== undefined) rec.multi = m.imageDataUrls;
            imgStore.put(rec);
          });
          for (const key of existing) {
            const idx = Number(key.slice(conv.id.length + 1));
            if (Number.isInteger(idx) && idx >= conv.messages.length) {
              imgStore.delete(key);
            }
          }
        }
        await txDone(tx);
      } catch (err) {
        fail(err);
        return fallback.put(conversations);
      }
    },

    async remove(ids) {
      if (broken) return fallback.remove(ids);
      try {
        const db = await openDb();
        const tx = db.transaction([CONV_STORE, IMAGE_STORE], 'readwrite');
        for (const id of ids) {
          tx.objectStore(CONV_STORE).delete(id);
          tx.objectStore(IMAGE_STORE).delete(imageRange(id));
        }
        await txDone(tx);
      } catch (err) {
        fail(err);
        return fallback.remove(ids);
      }
    },

    async clear() {
      if (broken) return fallback.clear();
      try {
        const db = await openDb();
        const tx = db.transaction([CONV_STORE, IMAGE_STORE], 'readwrite');
        tx.objectStore(CONV_STORE).clear();
        tx.objectStore(IMAGE_STORE).clear();
        await txDone(tx);
      } catch (err) {
        fail(err);
        return fallback.clear();
      }
    },
  };
}
