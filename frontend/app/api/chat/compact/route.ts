/**
 * POST /api/chat/compact — fold this conversation's older turns into its
 * rolling summary on demand ("Compact now" in the context-meter popover).
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ compacted: false, reason: 'mock mode' });
  }
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  try {
    const upstream = await fetch(`${orchestratorUrl}/chat/compact`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // Compaction is owner-scoped, so the session cookie must ride along.
        ...(req.headers.get('cookie')
          ? { cookie: req.headers.get('cookie') as string }
          : {}),
      },
      body: await req.text(),
    });
    return Response.json(await upstream.json(), { status: upstream.status });
  } catch {
    return Response.json(
      { compacted: false, reason: 'orchestrator unreachable' },
      { status: 502 },
    );
  }
}
