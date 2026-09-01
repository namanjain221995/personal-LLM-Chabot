/**
 * POST /api/auth/invitations/accept {token, name, password} — accept an
 * invitation. Success auto-logs-in (ME_PAYLOAD + Set-Cookie), which is why
 * this rides proxyToOrchestrator: the session cookie must reach the
 * browser. 404 = token invalid (all reasons identical), 422 = weak password.
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return handleMockAuth(req, ['invitations', 'accept']);
  }
  return proxyToOrchestrator(req, '/auth/invitations/accept');
}
