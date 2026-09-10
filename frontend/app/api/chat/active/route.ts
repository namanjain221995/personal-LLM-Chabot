/**
 * GET /api/chat/active — conversation ids the orchestrator is still
 * generating for. The sidebar polls this to show a spinner next to busy
 * chats (ChatGPT-style) and to re-attach after a page reload.
 *
 * 2026-09-10 (INF-2 / fe-chat F4): a transport failure is reported AS a
 * failure. This route used to answer 200 `{active: []}` whenever the
 * orchestrator could not be reached, which is the same answer it gives when
 * the server is healthy and idle — so a tab reloaded during a deploy's
 * recreate window (SIGTERM → drain → image start → migrations → health, with
 * the launcher recreating the orchestrator BEFORE the frontend) was told
 * "nothing is running" when the truth was "I could not ask". The browser then
 * skipped the re-attach and, on the next send, cancelled the generation that
 * was still running. "Could not ask" must be distinguishable from "nothing
 * running", so it now travels as 502 and the client renders status_unknown.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ active: [] });
  }
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  let upstream: Response;
  try {
    upstream = await fetch(`${orchestratorUrl}/chat/active`, {
      cache: 'no-store',
      // The cookie identifies the user: generations are owner-scoped, so
      // without it the orchestrator reports nothing.
      headers: req.headers.get('cookie')
        ? { cookie: req.headers.get('cookie') as string }
        : {},
    });
  } catch {
    return Response.json(
      { message: 'The orchestrator is unreachable.', code: 'NETWORK_ERROR' },
      { status: 502 },
    );
  }
  let body: unknown;
  try {
    body = await upstream.json();
  } catch {
    // An intermediary's own error page, or an empty body. The STATUS is the
    // fact; inventing an empty active list on top of it is what INF-2 was.
    if (upstream.ok) {
      return Response.json(
        { message: 'The orchestrator answered with no body.', code: 'UNKNOWN_ERROR' },
        { status: 502 },
      );
    }
    body = { message: 'The orchestrator refused the request.' };
  }
  return Response.json(body, { status: upstream.status });
}
