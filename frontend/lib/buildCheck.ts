/**
 * An old tab must not keep running old code after a deploy
 * (docs/chat-media/STORE-ALWAYS.md §3, 2026-10-03).
 *
 * What happened: the owner sent an invoice photo from a phone tab opened
 * before that night's deploy. The tab still ran the old JavaScript, which
 * never stored photos, so the chat said "Photo not stored on the server" 14
 * minutes after the deploy that stores them. The server now stores what an
 * old page sends; this is the other half — the page notices it is old.
 *
 * Every page carries the id of the build that served it (a `techsara-build`
 * meta, app/layout.tsx); GET /api/version answers the id of the build serving
 * now (lib/buildId.ts). The chat page compares them when it comes back into
 * view, on focus, and at most every five minutes while visible. When they
 * differ it reloads at once if nothing would be lost, and otherwise shows a
 * small banner and reloads by itself once the send in progress has finished
 * and the composer is empty (components/useBuildCheck.ts).
 *
 * No check storms: one request in flight at a time, none closer than
 * BUILD_CHECK_MIN_GAP_MS to the last, none while hidden, and a failure is
 * silent — an unknown answer is never taken for a new build.
 */

/** The `<meta name>` the page's own build id travels in. */
export const BUILD_META_NAME = 'techsara-build';
/** The timer's period while the page is visible. */
export const BUILD_CHECK_EVERY_MS = 5 * 60_000;
/**
 * A focus and a visibilitychange arrive together, and a person alt-tabbing
 * produces a stream of both: no check starts closer than this to the last.
 */
export const BUILD_CHECK_MIN_GAP_MS = 15_000;
/** Where typed text waits across a reload the person asked for. */
export const RELOAD_DRAFT_KEY = 'techsara.reloadDraft';
/** The server build this tab last reloaded for (the loop guard). */
export const RELOADED_FOR_KEY = 'techsara.reloadedFor';

/** The build that served this page, or null (dev, an older server). */
export function pageBuildId(doc: Document | undefined = globalThis.document): string | null {
  const content = doc
    ?.querySelector(`meta[name="${BUILD_META_NAME}"]`)
    ?.getAttribute('content');
  return content ? content : null;
}

/** The build the server runs now, or null when that could not be learned. */
export async function fetchServerBuildId(): Promise<string | null> {
  try {
    const res = await fetch('/api/version', { cache: 'no-store' });
    if (!res.ok) return null;
    const body = (await res.json()) as { build?: unknown };
    return typeof body.build === 'string' && body.build ? body.build : null;
  } catch {
    return null;
  }
}

export interface BuildCheckDeps {
  /** The build that served this page. */
  pageBuild: string;
  /** Called once, the first time the server names a different build. */
  onNewBuild: (serverBuild: string) => void;
  fetchBuild?: () => Promise<string | null>;
  now?: () => number;
}

/** The comparison, without timers or events — the hook owns those. */
export function createBuildCheck(deps: BuildCheckDeps) {
  const fetchBuild = deps.fetchBuild ?? fetchServerBuildId;
  const now = deps.now ?? Date.now;
  let inFlight = false;
  let lastAt = Number.NEGATIVE_INFINITY;
  let found = false;
  return {
    /** Ask the server, unless a check is running, ran just now, or already found one. */
    async check(): Promise<void> {
      if (found || inFlight || now() - lastAt < BUILD_CHECK_MIN_GAP_MS) return;
      inFlight = true;
      lastAt = now();
      try {
        const server = await fetchBuild();
        if (server && server !== deps.pageBuild) {
          found = true;
          deps.onNewBuild(server);
        }
      } finally {
        inFlight = false;
      }
    },
    get newBuild(): boolean {
      return found;
    },
  };
}

/** The reload itself — its own function so tests can stand in for it. */
export function reloadPage(): void {
  window.location.reload();
}

function sessionStore(): Storage | null {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

function here(): string {
  return `${window.location.pathname}${window.location.search}`;
}

/**
 * Keep typed text for the reload the banner's button is about to do. The
 * composer does not persist a draft, so this is the one reload that carries
 * it — keyed to this page's address, read back once, then gone.
 */
export function saveReloadDraft(text: string, storage: Storage | null = sessionStore()): void {
  if (!text.trim() || !storage) return;
  try {
    storage.setItem(RELOAD_DRAFT_KEY, JSON.stringify({ at: here(), text }));
  } catch {
    // Storage full or refused: the reload goes ahead; the banner said so.
  }
}

/**
 * The loop guard. A reload is made FOR a server build; if the page that comes
 * back still carries the old one (a cache between the browser and the
 * server), reloading again would only repeat itself, so a tab reloads by
 * itself at most once per server build. The banner still offers it.
 */
export function markReloadedFor(build: string, storage: Storage | null = sessionStore()): void {
  try {
    storage?.setItem(RELOADED_FOR_KEY, build);
  } catch {
    // Without storage there is no guard; a reload still needs a focus or the timer.
  }
}

/** The server build this tab already reloaded for, or null. */
export function reloadedFor(storage: Storage | null = sessionStore()): string | null {
  try {
    return storage?.getItem(RELOADED_FOR_KEY) ?? null;
  } catch {
    return null;
  }
}

/** Forget the guard once this page IS that build. */
export function clearReloadedFor(storage: Storage | null = sessionStore()): void {
  try {
    storage?.removeItem(RELOADED_FOR_KEY);
  } catch {
    // Nothing to forget.
  }
}

/** The text a reload of this page left behind, at most once. */
export function takeReloadDraft(storage: Storage | null = sessionStore()): string | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(RELOAD_DRAFT_KEY);
    if (raw === null) return null;
    storage.removeItem(RELOAD_DRAFT_KEY);
    const saved = JSON.parse(raw) as { at?: unknown; text?: unknown };
    return saved.at === here() && typeof saved.text === 'string' && saved.text.trim()
      ? saved.text
      : null;
  } catch {
    return null;
  }
}
