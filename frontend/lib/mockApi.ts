/**
 * MOCK_MODE=true auth + history + memory backend (V2 counterpart of the §9
 * chat fixtures): a tiny in-memory implementation of the orchestrator's
 * /auth, /history (V2 §3c) and /memory/facts contracts so the FULL v2 UI —
 * login, server history, migration, the memory panel — is demo-able before
 * the real backend exists.
 *
 * Server-only module (imported by route handlers). State lives for the
 * lifetime of the Node process; that is exactly right for a demo.
 */

import { FACT_NOT_FOUND, type MemoryFact } from './memory';
import type { MemoryProxyDecision } from './memoryRoutes';
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
 * The ME_PAYLOAD the mock serves — same shape as the real orchestrator's
 * /auth/me (enterprise auth retrofit) so the cache scoping (`u<id>`), the
 * capability-driven UI and the login flow are all demo-able in MOCK_MODE.
 */
const MOCK_ME = {
  username: MOCK_LOCAL_USER,
  user: { id: 1, name: 'Local User', email: 'local@techsara.test' },
  workspace: { id: 'ws-local', name: 'TechSara (mock)', role: 'super_admin' },
  capabilities: ['members.read', 'audit.read', 'workspace_content.read'],
  local: true,
};

/**
 * Mirrors orchestrator `authn/display_name.MAX_LENGTH`, so MOCK_MODE can show
 * the too-long refusal. Only the two rules a person meets by accident are
 * mocked; the punctuation allowlist and the prompt-instruction refusal live
 * on the server alone, because duplicating them here would give two places to
 * disagree about what a name is.
 */
const MOCK_NAME_MAX_LENGTH = 64;

/** The mock's session is the cookie's PRESENCE — mirrors the middleware. */
function hasMockSession(req: Request): boolean {
  return /(?:^|;\s*)ts_session=/.test(req.headers.get('cookie') ?? '');
}

const MOCK_SESSION_COOKIE =
  'ts_session=mock-session; Path=/; HttpOnly; SameSite=Lax';
const MOCK_SESSION_CLEAR =
  'ts_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax';

/**
 * Mock login/logout/me, so MOCK_MODE exercises the full session flow: any
 * email+password signs in (this is a demo, not a lock), logout clears the
 * cookie, and /me is 401 without one — exactly the contract the real
 * orchestrator serves, minus the credential check.
 */
export async function handleMockAuth(
  req: Request,
  path: string[],
): Promise<Response> {
  const endpoint = path.join('/');

  if (endpoint === 'me' && req.method === 'GET') {
    return hasMockSession(req)
      ? json(200, MOCK_ME)
      : json(401, { detail: 'Not signed in.' });
  }

  if (endpoint === 'login' && req.method === 'POST') {
    let body: { email?: unknown; password?: unknown } = {};
    try {
      body = (await req.json()) as typeof body;
    } catch {
      return json(422, { detail: 'Body must be JSON.' });
    }
    if (
      typeof body.email !== 'string' ||
      !body.email ||
      typeof body.password !== 'string' ||
      !body.password
    ) {
      return json(401, { detail: 'Incorrect email or password.' });
    }
    return json(200, MOCK_ME, MOCK_SESSION_COOKIE);
  }

  if (endpoint === 'logout' && req.method === 'POST') {
    // Safe when signed out, like the real endpoint.
    return json(200, { ok: true }, MOCK_SESSION_CLEAR);
  }

  if (endpoint === 'profile' && req.method === 'PATCH') {
    if (!hasMockSession(req)) return json(401, { detail: 'Sign in required.' });
    let body: { display_name?: unknown } = {};
    try {
      body = (await req.json()) as typeof body;
    } catch {
      return json(422, { detail: 'Body must be JSON.' });
    }
    const name =
      typeof body.display_name === 'string' ? body.display_name.trim() : '';
    if (!name) return json(422, { detail: 'Enter a name.' });
    if (name.length > MOCK_NAME_MAX_LENGTH) {
      return json(422, {
        detail: `A name can be at most ${MOCK_NAME_MAX_LENGTH} characters.`,
      });
    }
    // The mock's identity is process state, so the rename survives the next
    // GET /auth/me exactly as the database makes it survive in production.
    MOCK_ME.user.name = name;
    return json(200, { display_name: name, user: { ...MOCK_ME.user } });
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

/* ---------------------------------------------------------------- memory */

/**
 * Three saved facts, one of each provenance the panel labels (V40 `source`:
 * 'stated', 'manual', and NULL for a row written before provenance existed),
 * so every label and the excerpt quote are demo-able. The third is the kind
 * of row the old extraction rules wrote — a task request, not a fact about
 * the person — which is what the panel exists to let people remove.
 */
function seedMemory(): MemoryFact[] {
  return [
    {
      id: 101,
      fact: 'Works as a data engineer and mostly writes SQL',
      source_conversation_id: 'mock-conv-1',
      source: 'stated',
      source_excerpt: 'I work as a data engineer, so most of what I write is SQL',
      created_at: '2026-09-18T09:30:00Z',
      updated_at: '2026-09-18T09:30:00Z',
    },
    {
      id: 102,
      fact: 'Prefers answers in metric units',
      source_conversation_id: null,
      source: 'manual',
      source_excerpt: null,
      created_at: '2026-09-10T12:00:00Z',
      updated_at: '2026-09-10T12:00:00Z',
    },
    {
      id: 103,
      fact: 'Wants a cover letter for the Acme analyst role',
      source_conversation_id: 'mock-conv-0',
      source: null,
      source_excerpt: null,
      created_at: '2026-08-02T08:00:00Z',
      updated_at: '2026-08-02T08:00:00Z',
    },
  ];
}

const memoryByUser = new Map<string, MemoryFact[]>();

function userMemory(username: string): MemoryFact[] {
  let facts = memoryByUser.get(username);
  if (!facts) {
    facts = seedMemory();
    memoryByUser.set(username, facts);
  }
  return facts;
}

/**
 * /memory/facts for an ALREADY-ALLOWLISTED call (the route classifies first,
 * exactly as it does in live mode), answering in memory_api.py's shapes.
 */
export function handleMockMemory(
  decision: Exclude<MemoryProxyDecision, { kind: 'reject' }>,
): Response {
  const user = MOCK_LOCAL_USER;
  const facts = userMemory(user);

  if (decision.kind === 'list') {
    return json(200, { facts });
  }
  if (decision.kind === 'delete-one') {
    const id = Number(decision.id);
    const kept = facts.filter((f) => f.id !== id);
    if (kept.length === facts.length) {
      return json(404, { detail: FACT_NOT_FOUND });
    }
    memoryByUser.set(user, kept);
    return json(200, { deleted: id });
  }
  memoryByUser.set(user, []);
  return json(200, { deleted: facts.length });
}
