/**
 * /api/auth/preferences — the signed-in user's server-side preference blob:
 * GET → {prefs:{}}, PUT {prefs:{}} → {ok}.
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

async function handle(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return handleMockAuth(req, ['preferences']);
  }
  return proxyToOrchestrator(req, '/auth/preferences');
}

export async function GET(req: Request): Promise<Response> {
  return handle(req);
}

export async function PUT(req: Request): Promise<Response> {
  return handle(req);
}
