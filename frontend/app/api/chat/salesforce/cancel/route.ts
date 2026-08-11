/**
 * POST /api/chat/salesforce/cancel — drop a pending clarifying question.
 *
 * Called when the Salesforce source is switched off while a question is on
 * screen. The card disappearing is not enough: without this the server would
 * still be waiting, and the next Salesforce turn in that chat would be read as
 * an answer to a question the user had visibly dismissed.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ cancelled: 0 });
  }
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  try {
    const upstream = await fetch(`${orchestratorUrl}/chat/salesforce/cancel`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(req.headers.get('cookie')
          ? { cookie: req.headers.get('cookie') as string }
          : {}),
      },
      body: await req.text(),
    });
    return Response.json(await upstream.json(), { status: upstream.status });
  } catch {
    return Response.json({ cancelled: 0 }, { status: 502 });
  }
}
