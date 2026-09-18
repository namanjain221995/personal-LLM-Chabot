/**
 * /api/memory/* — the browser's way to the orchestrator's saved facts (B11):
 * list them, delete one, delete all. Scoped server-side to the signed-in user
 * (memory_api.py filters by the session's user inside the SQL); cookies travel
 * both ways through proxyToOrchestrator so the ts_session cookie authenticates.
 *
 * An allowlist, not a passthrough: lib/memoryRoutes names the three calls and
 * builds the upstream path itself, so every other path, method or query string
 * is a 404 here. POST/PUT/PATCH/OPTIONS are exported only so they answer
 * that 404 too. Without OPTIONS, Next's automatic handler answered 204 with
 * an Allow header listing every exported method (QA, 2026-09-18), which is
 * harmless but contradicts the point of a 404 for everything unlisted.
 * MOCK_MODE=true serves the in-memory mock instead.
 */

import { handleMockMemory } from '@/lib/mockApi';
import { classifyMemoryPath, upstreamMemoryPath } from '@/lib/memoryRoutes';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type Ctx = { params: Promise<{ path: string[] }> };

async function handle(req: Request, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  const decision = classifyMemoryPath(
    path ?? [],
    req.method,
    new URL(req.url).searchParams,
  );
  if (decision.kind === 'reject') {
    return Response.json({ message: 'Unknown memory endpoint.' }, { status: 404 });
  }

  if (process.env.MOCK_MODE === 'true') {
    return handleMockMemory(decision);
  }

  return proxyToOrchestrator(req, upstreamMemoryPath(decision));
}

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function DELETE(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function POST(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function PUT(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function PATCH(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function OPTIONS(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}
