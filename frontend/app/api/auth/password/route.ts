/**
 * POST /api/auth/password {current_password, new_password} — change the
 * signed-in user's password. Statuses pass through: 403 = wrong current
 * password, 422 = the new one is too weak (server-worded {detail}).
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return handleMockAuth(req, ['password']);
  }
  return proxyToOrchestrator(req, '/auth/password');
}
