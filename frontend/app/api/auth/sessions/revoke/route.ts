/**
 * POST /api/auth/sessions/revoke {session_id} | {others:true} — revoke one
 * of the signed-in user's sessions, or every one but this. → {revoked:n}
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return handleMockAuth(req, ['sessions', 'revoke']);
  }
  return proxyToOrchestrator(req, '/auth/sessions/revoke');
}
