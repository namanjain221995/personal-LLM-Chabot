/**
 * GET /api/uploads/[conversation] — list a conversation's stored uploads, so
 * a reloaded tab can tell which references still resolve before asking for
 * bytes. Proxy only; the orchestrator owns ownership and TTL truth.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const SAFE_CONVERSATION = /^[A-Za-z0-9_-]{1,64}$/;

export async function GET(
  req: Request,
  { params }: { params: Promise<{ conversation: string }> },
): Promise<Response> {
  const { conversation } = await params;
  let conv: string;
  try {
    conv = decodeURIComponent(conversation);
  } catch {
    return Response.json({ message: 'invalid conversation id' }, { status: 400 });
  }
  if (!SAFE_CONVERSATION.test(conv)) {
    return Response.json({ message: 'invalid conversation id' }, { status: 400 });
  }
  const orchestratorUrl = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  const cookie = req.headers.get('cookie');
  try {
    const upstream = await fetch(
      `${orchestratorUrl}/uploads/${encodeURIComponent(conv)}`,
      { signal: req.signal, headers: cookie ? { cookie } : {} },
    );
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: {
        'content-type': upstream.headers.get('content-type') ?? 'application/json',
        'cache-control': 'no-store',
      },
    });
  } catch {
    return Response.json({ message: 'upload service unreachable' }, { status: 502 });
  }
}
