/**
 * Server-side proxy helper for the /api/auth/* and /api/history/* route
 * handlers (V2 §4a). Forwards the request to the orchestrator and passes
 * cookies BOTH directions: the browser's Cookie header goes upstream, and
 * every upstream Set-Cookie comes back down (that is how the HttpOnly
 * ts_session cookie reaches the browser through the Next.js proxy).
 */

export function orchestratorUrl(): string {
  return process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
}

/** Read Set-Cookie headers portably (undici exposes getSetCookie()). */
function setCookiesOf(headers: Headers): string[] {
  const h = headers as Headers & { getSetCookie?: () => string[] };
  if (typeof h.getSetCookie === 'function') return h.getSetCookie();
  const single = headers.get('set-cookie');
  return single ? [single] : [];
}

export async function proxyToOrchestrator(
  req: Request,
  upstreamPath: string,
): Promise<Response> {
  const headers: Record<string, string> = {};
  const cookie = req.headers.get('cookie');
  if (cookie) headers.cookie = cookie;
  const contentType = req.headers.get('content-type');
  if (contentType) headers['content-type'] = contentType;

  let upstream: Response;
  try {
    upstream = await fetch(`${orchestratorUrl()}${upstreamPath}`, {
      method: req.method,
      headers,
      body:
        req.method === 'GET' || req.method === 'HEAD'
          ? undefined
          : await req.text(),
      cache: 'no-store',
      redirect: 'manual',
    });
  } catch {
    return Response.json(
      { message: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }

  const responseHeaders = new Headers();
  responseHeaders.set(
    'content-type',
    upstream.headers.get('content-type') ?? 'application/json',
  );
  responseHeaders.set('cache-control', 'no-store');
  for (const c of setCookiesOf(upstream.headers)) {
    responseHeaders.append('set-cookie', c);
  }

  return new Response(await upstream.arrayBuffer(), {
    status: upstream.status,
    headers: responseHeaders,
  });
}
