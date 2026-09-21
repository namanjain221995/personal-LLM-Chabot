/**
 * PATCH /api/auth/profile — the signed-in person's own display name.
 * {display_name} → {display_name, user:{id,name,email}} | 422 {detail} | 401.
 *
 * PATCH only: there is nothing here to GET (/api/auth/me already answers who
 * you are) and nothing to create, so a second verb would only be a second
 * surface to keep honest. Whose account it is comes from the ts_session cookie
 * this proxy forwards, never from the body — see orchestrator
 * app/authn/api.update_profile.
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function PATCH(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return handleMockAuth(req, ['profile']);
  }
  return proxyToOrchestrator(req, '/auth/profile');
}
