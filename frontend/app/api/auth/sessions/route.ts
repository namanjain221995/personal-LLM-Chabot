/**
 * GET /api/auth/sessions — the signed-in user's own sessions (current
 * flagged), for the "sign out other devices" surface.
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return handleMockAuth(req, ['sessions']);
  }
  return proxyToOrchestrator(req, '/auth/sessions');
}
