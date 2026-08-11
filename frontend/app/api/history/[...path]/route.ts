/**
 * /api/history/* — proxy to the orchestrator's history endpoints (V2 §3c):
 * conversation CRUD + message append, plus V4 §2's read-only chat search, all
 * scoped server-side to the signed-in user. Cookies are forwarded BOTH
 * directions so the orchestrator can authenticate the ts_session cookie.
 * MOCK_MODE=true serves the in-memory mock backend instead.
 */

import { handleMockHistory } from '@/lib/mockApi';
import { classifyHistoryPath } from '@/lib/historyRoutes';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type Ctx = { params: Promise<{ path: string[] }> };

async function handle(req: Request, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  const parts = path ?? [];

  const decision = classifyHistoryPath(parts, req.method);
  if (decision.kind === 'reject') {
    return Response.json(
      { message: 'Unknown history endpoint.' },
      { status: 404 },
    );
  }
  const isSearch = decision.kind === 'search';

  if (process.env.MOCK_MODE === 'true') {
    return handleMockHistory(req, parts);
  }

  const params = new URL(req.url).searchParams;

  if (isSearch) {
    // q + limit only — the proxy stays an allowlist, not a passthrough.
    const forwarded = new URLSearchParams({ q: params.get('q') ?? '' });
    const limit = params.get('limit');
    if (limit !== null) forwarded.set('limit', limit);
    return proxyToOrchestrator(req, `/history/search?${forwarded.toString()}`);
  }

  // V3 §1: ?archived=<bool> selects the archived list.
  const archived = params.get('archived');
  const query =
    parts.length === 1 && archived !== null
      ? `?archived=${encodeURIComponent(archived)}`
      : '';
  return proxyToOrchestrator(
    req,
    `/history/${parts.map(encodeURIComponent).join('/')}${query}`,
  );
}

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function POST(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function PUT(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function DELETE(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}
