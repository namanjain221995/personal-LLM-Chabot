/**
 * GET /api/files/mine/summary — how many files of each kind, and their size,
 * under the page's search, dates and size (orchestrator: GET
 * /files/mine/summary). The type filter's counts and the page's count line.
 */

import { filesProxy, notFound } from '@/lib/myfilesProxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request): Promise<Response> {
  return filesProxy(req, 'summary');
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
