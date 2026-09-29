/**
 * GET /api/audio/sessions/[id]/audio — the stored recording, for its owner.
 *
 * Streamed, with the Range header forwarded and Content-Range / 206 / 416
 * passed back, so a player can seek an hour-long recording without the proxy
 * holding 58 MB of it. Cache-Control is no-store: this is somebody's voice.
 */

import { SESSION_ID, forwardSession, notFound } from '../../_forward';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(
  req: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  if (!SESSION_ID.test(id)) return notFound();
  return forwardSession(req, `/audio/sessions/${id}/audio`);
}
