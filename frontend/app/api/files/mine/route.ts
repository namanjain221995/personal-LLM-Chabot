/**
 * GET /api/files/mine — the signed-in person's own uploads across every chat,
 * newest first, keyset-paged (orchestrator: GET /files/mine). Read-only; the
 * page's actions go to the routes that own each file. See lib/myfilesProxy.ts
 * for what is forwarded and why nothing else is.
 */

import { filesProxy, notFound } from '@/lib/myfilesProxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request): Promise<Response> {
  return filesProxy(req, 'list');
}

export async function POST(): Promise<Response> {
  return notFound();
}

export async function PUT(): Promise<Response> {
  return notFound();
}

export async function PATCH(): Promise<Response> {
  return notFound();
}

export async function DELETE(): Promise<Response> {
  return notFound();
}
