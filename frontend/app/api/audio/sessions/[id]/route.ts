/**
 * /api/audio/sessions/[id] — a session's state (GET, a long-poll of at most
 * 25 s) and the person's discard (DELETE, which removes the stored audio).
 *
 * The long-poll is why no heartbeat is needed: every request answers within
 * 25 s, far under Cloudflare's 125 s first-byte limit, and the recorder asks
 * again until the transcript is done however long that takes.
 */

import { SESSION_ID, forwardSession, notFound } from '../_forward';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type Context = { params: Promise<{ id: string }> };

async function forward(req: Request, { params }: Context): Promise<Response> {
  const { id } = await params;
  if (!SESSION_ID.test(id)) return notFound();
  return forwardSession(req, `/audio/sessions/${id}`, { kind: req.method === 'GET' ? 'poll' : 'plain' });
}

export { forward as GET, forward as DELETE };
