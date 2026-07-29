/**
 * GET /api/auth/me — who the app is running as.
 *
 * This is NOT a login check any more: there is no login. It reports the single
 * local account so the UI can label things, and so the history store keeps
 * scoping its localStorage cache by a stable name (change the name and cached
 * conversations are orphaned, which is why this endpoint stayed).
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(): Promise<Response> {
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';

  try {
    const upstream = await fetch(`${orchestratorUrl}/auth/me`, {
      cache: 'no-store',
    });
    if (!upstream.ok) {
      return Response.json(
        { message: `The orchestrator responded with status ${upstream.status}.` },
        { status: 502 },
      );
    }
    return Response.json(await upstream.json());
  } catch {
    return Response.json(
      { message: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }
}
