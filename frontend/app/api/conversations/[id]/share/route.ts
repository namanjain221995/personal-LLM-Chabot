/**
 * /api/conversations/{id}/share — the OWNER's controls.
 *
 * Authenticated, exactly like every other proxy here: the cookie travels and
 * the orchestrator decides. Ownership is checked there, not here, so hiding
 * the button is never what keeps a conversation private.
 */

import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type Ctx = { params: Promise<{ id: string }> };

async function target(ctx: Ctx, suffix = ''): Promise<string> {
  const { id } = await ctx.params;
  return `/conversations/${encodeURIComponent(id)}/share${suffix}`;
}

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  return proxyToOrchestrator(req, await target(ctx));
}

export async function POST(req: Request, ctx: Ctx): Promise<Response> {
  // `?refresh=1` republishes onto the same link; without it, create.
  const refresh = new URL(req.url).searchParams.get('refresh') === '1';
  return proxyToOrchestrator(req, await target(ctx, refresh ? '/refresh' : ''));
}

export async function PATCH(req: Request, ctx: Ctx): Promise<Response> {
  return proxyToOrchestrator(req, await target(ctx));
}

export async function DELETE(req: Request, ctx: Ctx): Promise<Response> {
  return proxyToOrchestrator(req, await target(ctx));
}
