/**
 * POST /api/upload — dataset upload proxy (Phase 4).
 *
 * Streams the multipart body straight through to the orchestrator. Images and
 * PDFs travel as base64 inside the chat body, which is fine at 10-25 MB but
 * would hold ~270 MB in memory for a 200 MB archive — so datasets get their
 * own streaming path and the chat request carries only an upload id.
 */

import {
  boundedBodyStream,
  declaredBodyOverLimit,
  isBodyTooLarge,
} from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/**
 * The largest single-shot upload this route will carry, 513 MiB (2026-09-12).
 *
 * The route had no bound at all, which for a streaming proxy means a caller
 * could keep sending for as long as the orchestrator kept writing. The number
 * comes from what the client can actually send in ONE request, read off the
 * two places that decide it — a smaller cap would break uploads that work
 * today, which is the one outcome worse than no cap:
 *
 *   · datasets never chunk. ChatApp.tsx posts the whole file here as one
 *     multipart body, up to MAX_DATASET_BYTES (Composer.tsx) = 512 MiB.
 *   · documents and videos chunk past CHUNK_THRESHOLD_BYTES = 90 MiB
 *     (lib/uploadDocument.ts), and every part of a chunked session goes to
 *     /api/upload/chunked/… — a different route — at CHUNK_PART_BYTES = 64 MiB
 *     each, so the largest body that can reach THIS handler is the 512 MiB
 *     dataset, not the 4 GiB video.
 *
 * Plus 1 MiB for multipart framing: the boundary, the filename, and the
 * conversation_id and purpose fields that travel beside the file.
 *
 * Bounding this costs nothing in memory. `boundedBodyStream` counts the bytes
 * as they pass and never holds them, so a 512 MiB upload still crosses this
 * process one chunk at a time, exactly as it did before.
 */
export const MAX_UPLOAD_BODY_BYTES = 512 * 1024 * 1024 + 1024 * 1024;

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ message: 'uploads are disabled in mock mode' }, { status: 404 });
  }
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  // Refused on the caller's own declaration first, so an oversized upload is
  // told no before a single byte is read.
  if (declaredBodyOverLimit(req, MAX_UPLOAD_BODY_BYTES)) {
    return Response.json({ detail: 'The upload is too large.' }, { status: 413 });
  }
  try {
    const upstream = await fetch(`${orchestratorUrl}/uploads`, {
      method: 'POST',
      // Pass the multipart body and its boundary through untouched.
      headers: {
        ...(req.headers.get('content-type')
          ? { 'content-type': req.headers.get('content-type') as string }
          : {}),
        ...(req.headers.get('cookie')
          ? { cookie: req.headers.get('cookie') as string }
          : {}),
      },
      body: req.body ? boundedBodyStream(req.body, MAX_UPLOAD_BODY_BYTES) : null,
      // Required by undici when streaming a request body.
      duplex: 'half',
      signal: req.signal,
    } as RequestInit & { duplex: 'half' });
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: { 'content-type': 'application/json' },
    });
  } catch (err) {
    // A body that ran past the cap fails the in-flight fetch rather than
    // returning: the refusal is the stream's, and it reaches here wrapped in
    // undici's own TypeError, so it is read out of the cause chain.
    if (isBodyTooLarge(err)) {
      return Response.json({ detail: 'The upload is too large.' }, { status: 413 });
    }
    return Response.json(
      { detail: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }
}
