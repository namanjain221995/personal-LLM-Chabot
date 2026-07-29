/**
 * GET /api/chat/active — conversation ids the orchestrator is still
 * generating for. The sidebar polls this to show a spinner next to busy
 * chats (ChatGPT-style) and to re-attach after a page reload.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ active: [] });
  }
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  try {
    const upstream = await fetch(`${orchestratorUrl}/chat/active`, {
      cache: 'no-store',
      // The cookie identifies the user: generations are owner-scoped, so
      // without it the orchestrator reports nothing.
      headers: req.headers.get('cookie')
        ? { cookie: req.headers.get('cookie') as string }
        : {},
    });
    return Response.json(await upstream.json(), { status: upstream.status });
  } catch {
    return Response.json({ active: [] });
  }
}
