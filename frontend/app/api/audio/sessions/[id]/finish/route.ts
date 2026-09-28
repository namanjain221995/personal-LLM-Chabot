/**
 * POST /api/audio/sessions/[id]/finish — the recorder has stopped and every
 * part has been acknowledged (or `last_part: null` when some never can be).
 * Idempotent upstream: a repeated finish returns the current state.
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
  return forwardSession(req, `/audio/sessions/${id}/finish`);
}
