/**
 * /api/audio/sessions — open a recording session (POST) or list the caller's
 * own stored recordings (GET). See ./_forward.ts for what this proxy does and,
 * as importantly, what it leaves to the orchestrator.
 *
 * POST is sent the moment the microphone is pressed, in parallel with the
 * browser's permission prompt, so no audio ever waits on it. A 404 whose
 * reason is not `voice_unavailable` (the orchestrator's `sessions_off`, or an
 * orchestrator older than the session contract) sends the recorder down the
 * legacy one-blob road through /api/audio/transcribe, which is unchanged.
 */

import { forwardSession } from './_forward';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  return forwardSession(req, '/audio/sessions');
}

export async function GET(req: Request): Promise<Response> {
  return forwardSession(req, '/audio/sessions');
}
