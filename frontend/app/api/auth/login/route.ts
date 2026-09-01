/**
 * POST /api/auth/login {email, password, remember?} — sign in.
 *
 * Through proxyToOrchestrator so the HttpOnly ts_session Set-Cookie the
 * orchestrator issues reaches the browser, and so a 401 (bad credentials)
 * or 429 (throttled) arrives as itself rather than a flattened proxy error.
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') return handleMockAuth(req, ['login']);
  return proxyToOrchestrator(req, '/auth/login');
}
