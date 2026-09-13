/**
 * Whether the person reading the docs may open the developer console.
 *
 * The docs header and drawer link to /api. The console answers a signed-in
 * member with a 404 (CONTRACT §6) and a signed-out reader with the sign-in
 * page, after which the same 404; a link shown to everyone sent most readers
 * to "This page could not be found" (re-audit, 2026-09-13). The link is now
 * drawn only for `api.console.access`.
 *
 * SERVER ONLY (it reaches next/headers through consoleSession). It is the
 * console's own gate asked the console's own question, never a second rule
 * that could drift from it: `consoleSession` is the resolver app/api/layout.tsx
 * refuses with.
 *
 * Served by app/api/docs/console-access/route.ts, which DocsShell asks after
 * it mounts (the docs HTML itself is publicly cacheable and must not vary by
 * reader). A signed-out reader resolves at once (no cookie, no upstream
 * call), and a stalled orchestrator — the documented failure mode — costs the
 * link after LINK_DEADLINE_MS rather than holding the request for the
 * resolver's 30 s ceiling. Every failure resolves `false`, never a rejection.
 */

import { consoleSession } from '@/components/devplatform/server';

/** How long the header waits for the answer before leaving the link out. */
export const LINK_DEADLINE_MS = 3000;

export function consoleLinkAllowed(
  resolve: () => Promise<{ state: string }> = consoleSession,
  deadlineMs: number = LINK_DEADLINE_MS,
): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<boolean>((done) => {
    timer = setTimeout(() => done(false), deadlineMs);
  });
  const answer = resolve().then(
    (session) => session.state === 'allowed',
    () => false,
  );
  return Promise.race([answer, deadline]).finally(() => clearTimeout(timer));
}
