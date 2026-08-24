/**
 * GET /api/reports/<filename> — download proxy for generated report files
 * (§8/§10). FileCards links here; the bytes live in the orchestrator's
 * REPORTS_DIR and are served by its GET /reports/{filename}.
 *
 * The orchestrator URL is read server-side only, so it is never exposed to
 * browser JavaScript — the same rule the /api/history and /api/upload
 * proxies follow.
 *
 * Filename validation is deliberately duplicated here even though
 * core/report_paths.resolve_report_file already rejects the same shapes
 * upstream: a traversal attempt should never become an outbound request in
 * the first place. Defense in depth, not a replacement.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type Ctx = { params: Promise<{ filename: string }> };

/**
 * Reject anything that is not a plain file name in REPORTS_DIR.
 *
 * Next.js has already percent-decoded the segment by the time it reaches
 * `params`, so `%2e%2e%2f` arrives as `../` and is caught by the separator
 * and traversal checks below. The raw form is re-checked too, because a
 * double-encoded `%252e%252e` decodes only once and would otherwise travel
 * upstream as the literal text `%2e%2e`.
 */
export function isSafeReportName(name: string): boolean {
  if (!name || !name.trim()) return false;
  if (name !== name.trim()) return false;
  if (name.includes('/') || name.includes('\\')) return false;
  if (name.includes('..')) return false;
  if (name.startsWith('.')) return false;
  if (name.includes('\0')) return false;
  // A still-encoded separator or dot-segment means someone double-encoded.
  if (/%2e|%2f|%5c|%00/i.test(name)) return false;
  return true;
}

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  const { filename } = await ctx.params;

  if (!isSafeReportName(filename)) {
    return Response.json({ message: 'Invalid report filename.' }, { status: 400 });
  }

  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';

  let upstream: Response;
  try {
    upstream = await fetch(
      `${orchestratorUrl}/reports/${encodeURIComponent(filename)}`,
      {
        method: 'GET',
        headers: {
          // Forward the session cookie for parity with the other proxies;
          // /reports is auth-free today but must not break if that changes.
          ...(req.headers.get('cookie')
            ? { cookie: req.headers.get('cookie') as string }
            : {}),
        },
        cache: 'no-store',
        redirect: 'manual',
        signal: req.signal,
      },
    );
  } catch {
    return Response.json(
      { message: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }

  // A missing or rejected report is NOT a proxy failure: pass the upstream
  // status through so the browser sees 404/400 rather than a blanket 502.
  // (H-05 owns the global error-surface work; this route only needs its own
  // statuses to be truthful.)
  if (!upstream.ok || !upstream.body) {
    const status = upstream.status === 200 ? 502 : upstream.status;
    return Response.json(
      {
        message:
          status === 404
            ? 'That report file no longer exists.'
            : 'That report file could not be downloaded.',
      },
      { status },
    );
  }

  const headers = new Headers();
  headers.set(
    'content-type',
    upstream.headers.get('content-type') ?? 'application/octet-stream',
  );
  // Keeps the download named after the report rather than "[filename]".
  const disposition = upstream.headers.get('content-disposition');
  if (disposition) headers.set('content-disposition', disposition);
  // Only safe because the body below is piped through byte-for-byte.
  const length = upstream.headers.get('content-length');
  if (length) headers.set('content-length', length);
  headers.set('cache-control', 'no-store');

  return new Response(upstream.body, { status: upstream.status, headers });
}
