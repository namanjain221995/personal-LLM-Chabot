/**
 * POST /api/audio/sessions/[id]/retranscribe — "Retry" on a saved recording.
 * The audio is stored, so the server re-reads it; nobody is asked to speak
 * again.
 */

import { SESSION_ID, forwardSession, notFound } from '../../_forward';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(
  req: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  if (!SESSION_ID.test(id)) return notFound();
  return forwardSession(req, `/audio/sessions/${id}/retranscribe`);
}
