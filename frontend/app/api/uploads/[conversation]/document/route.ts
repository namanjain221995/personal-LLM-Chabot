/**
 * GET /api/uploads/[conversation]/document?name=… — Phase 3's companion
 * lookup: fetch a stored document's bytes for preview by conversation and
 * original filename. Same shape rules, same session forwarding, same honest
 * status passthrough as the [upload]/file proxy next door.
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
  const name = new URL(req.url).searchParams.get('name') ?? '';
  if (!name || name.length > 255 || name.includes('/') || name.includes('\\')) {
    return Response.json({ message: 'invalid document name' }, { status: 400 });
  }

  const orchestratorUrl = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  const cookie = req.headers.get('cookie');
  let upstream: Response;
  try {
    upstream = await fetch(
      `${orchestratorUrl}/uploads/${encodeURIComponent(conv)}/document?name=${encodeURIComponent(name)}`,
      { signal: req.signal, headers: cookie ? { cookie } : {} },
    );
  } catch {
    return Response.json({ message: 'upload service unreachable' }, { status: 502 });
  }
  if (!upstream.ok) {
    return Response.json({ message: 'document unavailable' }, { status: upstream.status });
  }
  const headers = new Headers({ 'cache-control': 'no-store' });
  for (const h of ['content-type', 'content-disposition', 'content-length']) {
    const v = upstream.headers.get(h);
    if (v) headers.set(h, v);
  }
  return new Response(upstream.body, { status: 200, headers });
}
