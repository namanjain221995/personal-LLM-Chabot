/**
 * PUT /api/audio/sessions/[id]/parts/[seq] — the next bytes of the recording,
 * sent while the person is still talking.
 *
 * The body is STREAMED to the orchestrator, never buffered here. Its SHA-256
 * (X-Part-SHA256) rides through untouched: only the orchestrator sees the
 * bytes land, so only it can check them, and a 200 from it means the part is
 * on disk. The browser deletes a part from its outbox on that 200 and on
 * nothing else, so anything short of it from here (the 502 included) leaves
 * the part to be sent again.
 */

import { PART_SEQ, SESSION_ID, badRequest, forwardSession, notFound } from '../../../_forward';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function PUT(
  req: Request,
  { params }: { params: Promise<{ id: string; seq: string }> },
): Promise<Response> {
  const { id, seq } = await params;
  if (!SESSION_ID.test(id)) return notFound();
  if (!PART_SEQ.test(seq)) return badRequest('The part number must be a non-negative integer.');
  return forwardSession(req, `/audio/sessions/${id}/parts/${seq}`);
}
