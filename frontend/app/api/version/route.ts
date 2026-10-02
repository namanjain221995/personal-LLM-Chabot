/**
 * GET /api/version — the build this server runs: `{"build": "<id>" | null}`
 * (STORE-ALWAYS §3, 2026-10-03).
 *
 * The chat page compares it with the build that served the page (the
 * `techsara-build` meta, app/layout.tsx) when it comes back into view, on
 * focus and every five minutes, and reloads when they differ — so a tab
 * opened before a deploy stops running the old JavaScript. No session is
 * read and nothing but the id is said (every page already carries it); the
 * orchestrator is not asked. `no-store`, so no cache between the page and
 * this answer can hide a deploy.
 */

import { serverBuildId } from '@/lib/buildId';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export function GET(): Response {
  return Response.json(
    { build: serverBuildId() },
    { headers: { 'cache-control': 'no-store' } },
  );
}
