/**
 * POST /api/audio/transcribe — the microphone's proxy to the local ASR engine.
 *
 * Streams the recording straight through as the request body: it is already
 * compressed Opus, and buffering it here would add a copy and a memory
 * ceiling for nothing. The two scalars beside it — how long the browser
 * recorded, and which language to force — ride in the query string, so this
 * handler forwards that verbatim rather than parsing anything.
 *
 * Authorization lives UPSTREAM — the orchestrator requires a session and the
 * voice-input feature, and answers 401/403/404 itself. This handler adds
 * nothing but transport, so there is no way for it to disagree with the gate.
 *
 * NOTHING IS KEPT HERE. The body is a stream that ends when the response does.
 *
 * THE ANSWER IS STREAMED TOO (2026-09-18). A long recording decodes for minutes
 * (595 s of audio took 268.3 s), and the orchestrator keeps the connection
 * alive with a whitespace byte every 15 s before the JSON. Buffering it here
 * with `await upstream.text()` held every one of those bytes back until the
 * transcript existed — the exact silence Cloudflare ends at 125 s. The body is
 * handed through as it arrives; `no-transform` stops a compressing hop from
 * collecting it first.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json(
      { detail: 'voice input is disabled in mock mode' },
      { status: 404 },
    );
  }
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  // Forwarded whole: dropping it would silently transcribe every recording as
  // duration zero, which is the number the admin console reports as minutes
  // spoken.
  const { search } = new URL(req.url);
  try {
    const upstream = await fetch(`${orchestratorUrl}/audio/transcribe${search}`, {
      method: 'POST',
      headers: {
        ...(req.headers.get('content-type')
          ? { 'content-type': req.headers.get('content-type') as string }
          : {}),
        ...(req.headers.get('cookie')
          ? { cookie: req.headers.get('cookie') as string }
          : {}),
      },
      body: req.body,
      // Required by undici when streaming a request body.
      duplex: 'half',
      // The browser aborting a recording must abort the upload with it,
      // rather than leaving a GPU transcribing something nobody will read.
      signal: req.signal,
    } as RequestInit & { duplex: 'half' });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        'content-type': 'application/json',
        'cache-control': 'no-store, no-transform',
        'x-accel-buffering': 'no',
      },
    });
  } catch {
    return Response.json(
      { detail: 'The transcription service is unreachable.' },
      { status: 502 },
    );
  }
}
