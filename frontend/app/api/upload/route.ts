/**
 * POST /api/upload — dataset upload proxy (Phase 4).
 *
 * Streams the multipart body straight through to the orchestrator. Images and
 * PDFs travel as base64 inside the chat body, which is fine at 10-25 MB but
 * would hold ~270 MB in memory for a 200 MB archive — so datasets get their
 * own streaming path and the chat request carries only an upload id.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ message: 'uploads are disabled in mock mode' }, { status: 404 });
  }
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
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
      body: req.body,
      // Required by undici when streaming a request body.
      duplex: 'half',
      signal: req.signal,
    } as RequestInit & { duplex: 'half' });
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: { 'content-type': 'application/json' },
    });
  } catch {
    return Response.json(
      { detail: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }
}
