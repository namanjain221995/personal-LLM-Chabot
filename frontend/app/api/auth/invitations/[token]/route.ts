/**
 * GET /api/auth/invitations/{token} — preview an invitation before
 * accepting it (email, name, role, workspace, expiry). Expired, used,
 * revoked and unknown tokens are all the same 404 upstream, deliberately —
 * nothing here may help someone probe which tokens exist.
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type Ctx = { params: Promise<{ token: string }> };

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  const { token } = await ctx.params;
  if (process.env.MOCK_MODE === 'true') {
    return handleMockAuth(req, ['invitations', token]);
  }
  return proxyToOrchestrator(
    req,
    `/auth/invitations/${encodeURIComponent(token)}`,
  );
}
