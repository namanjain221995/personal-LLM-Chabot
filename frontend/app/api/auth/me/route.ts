/**
 * GET /api/auth/me — who the session belongs to (ME_PAYLOAD | 401).
 *
 * Reworked for the enterprise auth retrofit: the old version did a BARE
 * fetch — no cookie in either direction — and collapsed every failure onto
 * 502, so under real sessions it always resolved anonymous and a 401 could
 * never reach the browser. Now it rides proxyToOrchestrator like the rest
 * of /api/auth/*: the ts_session cookie goes upstream, any Set-Cookie comes
 * back down, and the status — 401 included — passes through honestly.
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') return handleMockAuth(req, ['me']);
  return proxyToOrchestrator(req, '/auth/me');
}
