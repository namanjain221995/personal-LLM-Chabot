/**
 * POST /api/chat/stop — cancel a detached generation server-side. Closing the
 * SSE stream no longer stops the model (generations keep running so they
 * survive reloads), so the Stop button calls this explicitly.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ stopped: false });
  }
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  try {
    const upstream = await fetch(`${orchestratorUrl}/chat/stop`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // Owner-scoped: only the user who started a generation may stop it.
        ...(req.headers.get('cookie')
          ? { cookie: req.headers.get('cookie') as string }
          : {}),
      },
      body: await req.text(),
    });
    return Response.json(await upstream.json(), { status: upstream.status });
  } catch {
    return Response.json({ stopped: false }, { status: 502 });
  }
}
