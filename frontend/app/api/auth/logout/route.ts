/**
 * POST /api/auth/logout — revoke the session and clear the cookie.
 *
 * Through proxyToOrchestrator so the clearing Set-Cookie reaches the
 * browser. Safe to call signed out (the orchestrator answers {ok:true}).
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') return handleMockAuth(req, ['logout']);
  return proxyToOrchestrator(req, '/auth/logout');
}
