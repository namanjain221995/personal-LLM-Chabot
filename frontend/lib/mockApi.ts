/**
 * MOCK_MODE=true auth + history backend (V2 counterpart of the §9 chat
 * fixtures): a tiny in-memory implementation of the orchestrator's /auth and
 * /history contracts (V2 §3c) so the FULL v2 UI — login, server history,
 * migration — is demo-able before the real backend exists.
 *
 * Server-only module (imported by route handlers). State lives for the
 * lifetime of the Node process; that is exactly right for a demo.
 */

import { buildSnippet, SEARCH_MAX_QUERY } from './searchPalette';

interface MockMessage {
  role: string;
  content: string;
  meta: unknown;
}

interface MockConversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  /** V3 §1 — mirrors the orchestrator's INTEGER NOT NULL DEFAULT 0 columns. */
  pinned: boolean;
  archived: boolean;
  messages: MockMessage[];
}

const convsByUser = new Map<string, Map<string, MockConversation>>();

/** Single local user — mock mode mirrors the real single-user orchestrator. */
const MOCK_LOCAL_USER = 'local';

function json(status: number, body: unknown, cookie?: string): Response {
  const headers = new Headers({
    'content-type': 'application/json',
    'cache-control': 'no-store',
  });
  if (cookie) headers.append('set-cookie', cookie);
  return new Response(JSON.stringify(body), { status, headers });
}

/* ------------------------------------------------------------------ auth */

/**
 * There is no login in mock mode either — only "who am I". Kept so MOCK_MODE
 * exercises the same single-user shape the real orchestrator now serves.
 */
export async function handleMockAuth(
  req: Request,
  path: string[],
): Promise<Response> {
  if (path.join('/') === 'me' && req.method === 'GET') {
    return json(200, { username: MOCK_LOCAL_USER, local: true });
  }
  return json(404, { detail: 'Unknown auth endpoint.' });
}

/* --------------------------------------------------------------- history */

function nowIso(): string {
  return new Date().toISOString();
}

/** Fields a PUT may carry (V3 §1); anything else is a 422. */
const PATCHABLE = ['title', 'pinned', 'archived'] as const;

function summaryOf(conv: MockConversation) {
  return {
    id: conv.id,
    title: conv.title,
    created_at: conv.created_at,
    updated_at: conv.updated_at,
    pinned: conv.pinned,
    archived: conv.archived,
  };
}

function userConvs(username: string): Map<string, MockConversation> {
  let map = convsByUser.get(username);
  if (!map) {
    map = new Map();
    convsByUser.set(username, map);
  }
  return map;
}

/** V4 §2 search hit; `snippet` is null for title-only matches. */
interface MockSearchResult {
  id: string;
  title: string;
  updated_at: string;
  pinned: boolean;
  archived: boolean;
  snippet: string | null;
  matched_in: 'title' | 'message';
}

const SEARCH_LIMIT_DEFAULT = 50;
const SEARCH_LIMIT_MAX = 100;

/**
 * GET /history/search?q=&limit= (V4 §2) — case-insensitive substring over
 * titles AND message content, one row per conversation, archived included and
 * flagged. `%`/`_` need no escaping here: this is a plain JS substring match,
 * never SQL, so wildcards are already literal.
 */
function mockSearch(
  convs: Map<string, MockConversation>,
  url: URL,
): Response {
  const q = (url.searchParams.get('q') ?? '').trim().slice(0, SEARCH_MAX_QUERY);
  if (!q) return json(200, { results: [] });

  const parsed = Number.parseInt(url.searchParams.get('limit') ?? '', 10);
  const limit =
    Number.isFinite(parsed) && parsed > 0
      ? Math.min(parsed, SEARCH_LIMIT_MAX)
      : SEARCH_LIMIT_DEFAULT;

  const needle = q.toLowerCase();
  const results: MockSearchResult[] = [];
  for (const conv of convs.values()) {
    const titleHit = conv.title.toLowerCase().includes(needle);
    const messageHit = conv.messages.find((m) =>
      m.content.toLowerCase().includes(needle),
    );
    if (!titleHit && !messageHit) continue;
    results.push({
      id: conv.id,
      title: conv.title,
      updated_at: conv.updated_at,
      pinned: conv.pinned,
      archived: conv.archived,
      snippet:
        titleHit || !messageHit ? null : buildSnippet(messageHit.content, q),
      matched_in: titleHit ? 'title' : 'message',
    });
  }

  // Pinned first, then most recently updated (V4 §2).
  results.sort(
    (a, b) =>
      Number(b.pinned) - Number(a.pinned) ||
      (a.updated_at < b.updated_at ? 1 : -1),
  );
  return json(200, { results: results.slice(0, limit) });
}

export async function handleMockHistory(
  req: Request,
  path: string[],
): Promise<Response> {
  const user = MOCK_LOCAL_USER;

  if (path.length === 1 && path[0] === 'search' && req.method === 'GET') {
    return mockSearch(userConvs(user), new URL(req.url));
  }

  if (path[0] !== 'conversations') {
    return json(404, { detail: 'Unknown history endpoint.' });
  }
  const convs = userConvs(user);

  // GET /history/conversations?archived=<bool> (V3 §1: default false)
  if (path.length === 1 && req.method === 'GET') {
    const wantArchived =
      new URL(req.url).searchParams.get('archived') === 'true';
    const list = [...convs.values()]
      .filter((c) => c.archived === wantArchived)
      // Pinned first, then most recently updated (V3 §1).
      .sort(
        (a, b) =>
          Number(b.pinned) - Number(a.pinned) ||
          (a.updated_at < b.updated_at ? 1 : -1),
      )
      .map(({ id, title, created_at, updated_at, pinned, archived }) => ({
        id,
        title,
        created_at,
        updated_at,
        pinned,
        archived,
      }));
    return json(200, list);
  }

  // POST /history/conversations {id?, title}
  if (path.length === 1 && req.method === 'POST') {
    let body: { id?: unknown; title?: unknown } = {};
    try {
      body = (await req.json()) as typeof body;
    } catch {
      return json(422, { detail: 'Body must be JSON.' });
    }
    const id =
      typeof body.id === 'string' && body.id
        ? body.id
        : `srv-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    const title = typeof body.title === 'string' && body.title ? body.title : 'New chat';
    const existing = convs.get(id);
    if (existing) {
      existing.title = title;
      existing.updated_at = nowIso();
      return json(200, summaryOf(existing));
    }
    const conv: MockConversation = {
      id,
      title,
      created_at: nowIso(),
      updated_at: nowIso(),
      pinned: false,
      archived: false,
      messages: [],
    };
    convs.set(id, conv);
    return json(200, summaryOf(conv));
  }

  const id = path[1];
  const conv = id ? convs.get(id) : undefined;

  // /history/conversations/{id}/messages
  if (path.length === 3 && path[2] === 'messages' && req.method === 'POST') {
    if (!conv) return json(404, { detail: 'Conversation not found.' });
    let body: { role?: unknown; content?: unknown; meta?: unknown } = {};
    try {
      body = (await req.json()) as typeof body;
    } catch {
      return json(422, { detail: 'Body must be JSON.' });
    }
    if (typeof body.role !== 'string' || typeof body.content !== 'string') {
      return json(422, { detail: 'Messages need a role and content.' });
    }
    conv.messages.push({
      role: body.role,
      content: body.content,
      meta: body.meta ?? null,
    });
    conv.updated_at = nowIso();
    return json(200, {});
  }

  if (path.length === 2 && id) {
    if (req.method === 'GET') {
      if (!conv) return json(404, { detail: 'Conversation not found.' });
      return json(200, {
        id: conv.id,
        title: conv.title,
        messages: conv.messages,
      });
    }
    // PUT {title?, pinned?, archived?} — any subset (V3 §1).
    if (req.method === 'PUT') {
      if (!conv) return json(404, { detail: 'Conversation not found.' });
      let body: Record<string, unknown> = {};
      try {
        body = (await req.json()) as Record<string, unknown>;
      } catch {
        return json(422, { detail: 'Body must be JSON.' });
      }
      const unknown = Object.keys(body).filter(
        (k) => !PATCHABLE.includes(k as (typeof PATCHABLE)[number]),
      );
      if (unknown.length > 0) {
        return json(422, { detail: `Unknown field: ${unknown[0]}` });
      }
      for (const flag of ['pinned', 'archived'] as const) {
        if (body[flag] === undefined) continue;
        if (typeof body[flag] !== 'boolean') {
          return json(422, { detail: `${flag} must be a boolean.` });
        }
        // Flag only: archiving must not disturb updated_at ordering (V3 §1).
        conv[flag] = body[flag];
      }
      if (typeof body.title === 'string' && body.title.trim()) {
        conv.title = body.title.trim();
        conv.updated_at = nowIso();
      }
      return json(200, summaryOf(conv));
    }
    if (req.method === 'DELETE') {
      if (!conv) return json(404, { detail: 'Conversation not found.' });
      convs.delete(id);
      return json(200, {});
    }
  }

  return json(404, { detail: 'Unknown history endpoint.' });
}
