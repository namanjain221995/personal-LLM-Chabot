/**
 * The console's SERVER-SIDE gate.
 *
 * CONTRACT §17 is explicit: "/api is server-guarded: the page resolves the
 * session server-side and refuses without api.console.access — never by
 * hiding a menu item." So this module runs on the server, before any console
 * markup is produced, and the page it guards renders nothing at all for a
 * member who types the URL directly.
 *
 * Three refusals, three different truths, and the difference matters:
 *
 *   signed-out   no cookie, or the orchestrator says 401 → /login, the same
 *                bounce every other page gives.
 *   refused      a real session WITHOUT api.console.access → notFound().
 *                404, not 403, because CONTRACT §6 says the console must not
 *                disclose its own existence to someone who may not use it —
 *                the same rule the admin surface already follows.
 *   unavailable  the orchestrator could not be asked. This is NOT a refusal.
 *                Answering 404 here would tell an admin their console had
 *                been taken away because a container was restarting; the
 *                admin layout has said "the server could not be reached" for
 *                exactly this case since the auth retrofit, and so does this.
 *
 * Nothing here is cached. Capabilities are recomputed per request upstream
 * (CONTRACT §6), so a demotion takes effect on the next page load, and a
 * cached "allowed" would be the one thing that could keep a removed admin's
 * console alive.
 */

import { PROXY_TIMEOUT_MS, orchestratorUrl } from '@/lib/proxy';
import { parseMe, type Me } from '@/components/admin/api';

/** rbac.Cap.API_CONSOLE_ACCESS — the capability this whole surface hangs on. */
export const API_CONSOLE_ACCESS = 'api.console.access';

export type ConsoleSession =
  | { state: 'signed-out' }
  | { state: 'refused' }
  | { state: 'unavailable'; reason: string }
  | { state: 'allowed'; me: Me };

export const CONSOLE_UNAVAILABLE =
  'The server could not be reached. Check the connection and retry.';

/**
 * Resolve who is asking, and whether the console exists for them.
 *
 * `fetchImpl` is injectable for the same reason `nav` is: the rule has to be
 * testable without a running orchestrator, and a gate nobody can test is a
 * gate nobody can trust.
 */
export async function resolveConsoleSession(
  cookieHeader: string | null,
  fetchImpl: typeof fetch = fetch,
  timeoutMs: number = PROXY_TIMEOUT_MS,
): Promise<ConsoleSession> {
  // No cookie at all is signed out by definition; asking upstream would be a
  // round trip whose only possible answer is 401.
  if (!cookieHeader) return { state: 'signed-out' };

  // A CEILING AND NO REDIRECTS (2026-09-13). This call had neither, so a
  // stalled orchestrator — the documented failure mode, /health green and
  // generation frozen (the 22:15Z outage, 2026-09-12) — held every /api page
  // render on undici's 300 s default instead of rendering the "could not be
  // reached" panel this module exists to give. lib/proxy.ts fixed the same
  // defect with the same 30 s bound. `redirect: 'manual'` because /auth/me
  // answers JSON; a 3xx is not an identity and must not be followed with the
  // session cookie attached. The race is explicit rather than trusting the
  // injected fetch to honour the signal — a fetch that ignores it must not be
  // able to hold the page either.
  let timer: ReturnType<typeof setTimeout> | undefined;
  const controller = new AbortController();
  const deadline = new Promise<'timeout'>((resolve) => {
    timer = setTimeout(() => {
      controller.abort();
      resolve('timeout');
    }, timeoutMs);
  });

  let body: unknown;
  try {
    const outcome = await Promise.race([
      (async () => {
        const res = await fetchImpl(`${orchestratorUrl()}/auth/me`, {
          headers: { cookie: cookieHeader },
          cache: 'no-store',
          redirect: 'manual',
          signal: controller.signal,
        });
        if (res.status === 401) return 'signed-out' as const;
        if (!res.ok) return 'unavailable' as const;
        // Inside the race too: headers on time and a body that never finishes
        // is the same stall.
        return { json: (await res.json()) as unknown };
      })(),
      deadline,
    ]);
    if (outcome === 'signed-out') return { state: 'signed-out' };
    if (outcome === 'timeout' || outcome === 'unavailable') {
      return { state: 'unavailable', reason: CONSOLE_UNAVAILABLE };
    }
    body = outcome.json;
  } catch {
    return { state: 'unavailable', reason: CONSOLE_UNAVAILABLE };
  } finally {
    clearTimeout(timer);
  }

  const me = parseMe(body);
  // An unparseable ME_PAYLOAD is not a refusal either — it is a version skew
  // between two containers, and it should read as "could not be checked".
  if (!me) return { state: 'unavailable', reason: CONSOLE_UNAVAILABLE };
  if (!me.capabilities.includes(API_CONSOLE_ACCESS)) return { state: 'refused' };
  return { state: 'allowed', me };
}
