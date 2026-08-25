/**
 * GET /api/debug/error?status=<code> — DEVELOPMENT ONLY.
 *
 * Answers with exactly what the chat proxy answers for a real failure: the
 * true status and `{code}`, and nothing else. That is the point — it is a
 * stand-in for a broken upstream, so anything that consumes it exercises the
 * genuine client path rather than a special case.
 *
 * In a production build this route answers 404, the same as a URL with no
 * handler behind it. The gate is the build mode alone (lib/devErrors.ts):
 * there is no header or parameter that can turn it on.
 *
 * Touches nothing. No database, no orchestrator, no model — it returns a
 * response and writes one log line.
 *
 *   /api/debug/error?status=404   → 404 NOT_FOUND
 *   /api/debug/error?status=500   → 500 APPLICATION_ERROR
 *   /api/debug/error?status=502   → 502 MODEL_UNAVAILABLE
 *   /api/debug/error?status=503   → 503 ORCHESTRATOR_UNAVAILABLE
 *   /api/debug/error?status=504   → 504 TIMEOUT
 *   /api/debug/error?status=network → 502 NETWORK_ERROR (no upstream status)
 */
import { categoryForStatus } from '@/lib/errorTypes';
import { parseSimulation, simulationEnabled } from '@/lib/devErrors';
import { logProxyError, requestIdOf } from '@/lib/serverLog';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(req: Request): Promise<Response> {
  // Not "403 forbidden" — absent. A production deployment must look exactly
  // as it would if this file had never been written.
  if (!simulationEnabled()) return new Response(null, { status: 404 });

  const startedAt = Date.now();
  const url = new URL(req.url);
  const simulation = parseSimulation(url.searchParams.get('status'));

  if (!simulation) {
    return Response.json(
      {
        message:
          'Pass ?status= a 4xx/5xx code, or "network" for a transport failure.',
        examples: [
          '/api/debug/error?status=404',
          '/api/debug/error?status=500',
          '/api/debug/error?status=502',
          '/api/debug/error?status=503',
          '/api/debug/error?status=504',
          '/api/debug/error?status=network',
        ],
      },
      { status: 400 },
    );
  }

  const status = simulation.kind === 'network' ? null : simulation.status;
  const category =
    simulation.kind === 'network' ? 'NETWORK_ERROR' : categoryForStatus(status);

  logProxyError({
    route: '/api/debug/error',
    status,
    category,
    message: `status=${url.searchParams.get('status')} requested`,
    requestId: requestIdOf(req),
    durationMs: Date.now() - startedAt,
    retryable: true,
    simulated: true,
  });

  // The same body shape the chat proxy returns, so a client cannot tell a
  // simulated failure from a real one — which is what makes it a useful test.
  return Response.json({ code: category }, { status: status ?? 502 });
}
