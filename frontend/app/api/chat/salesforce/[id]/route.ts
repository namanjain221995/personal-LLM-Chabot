/**
 * GET /api/chat/salesforce/{conversationId} — Salesforce Intelligence state.
 *
 * Two things the browser cannot know on its own after a reload: the clarifying
 * question this conversation is waiting on, and which Salesforce areas this
 * connection can actually reach (the starter card must never offer an object
 * the integration user cannot query).
 *
 * A failure here degrades to "no pending question, no options" rather than an
 * error: a missing starter card is a smaller problem than a chat that will not
 * open.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const EMPTY = { enabled: false, options: [], pending_clarification: null };

export async function GET(
  req: Request,
  ctx: { params: Promise<{ id: string }> },
): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json(EMPTY);
  }
  const { id } = await ctx.params;
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  try {
    const upstream = await fetch(
      `${orchestratorUrl}/chat/salesforce/${encodeURIComponent(id)}`,
      {
        cache: 'no-store',
        // Owner-scoped upstream: without the cookie the orchestrator refuses
        // to say what another account's conversation is asking about.
        headers: req.headers.get('cookie')
          ? { cookie: req.headers.get('cookie') as string }
          : {},
      },
    );
    if (upstream.status === 401) {
      // Session death must be visible — an empty payload here would quietly
      // strip the starter card from a signed-out tab instead of prompting a
      // re-login through the app's 401 handling.
      return Response.json({ message: 'Sign in required.' }, { status: 401 });
    }
    if (!upstream.ok) return Response.json(EMPTY);
    return Response.json(await upstream.json());
  } catch {
    return Response.json(EMPTY);
  }
}
