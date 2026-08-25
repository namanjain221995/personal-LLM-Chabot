/**
 * DEVELOPMENT-ONLY error simulation.
 *
 * The error page is the hardest thing in the app to see on purpose: making a
 * real 504 happen means breaking a real service, and "orchestrator down" is
 * not something to reproduce on a machine someone else is using. This module
 * lets a developer ask for a specific failure and get it through the REAL
 * path — the same proxy, the same classification, the same page — without
 * touching a service, a model or the database.
 *
 * PRODUCTION SAFETY. `simulationEnabled()` is the single gate, and everything
 * that can simulate a failure checks it first. In a production build
 * NODE_ENV is "production", the gate is closed, and the debug route answers
 * 404 exactly as if the file were not deployed. There is no header, cookie or
 * query parameter that can open it — the only input is the build mode.
 */

/** The one gate. Closed in production builds; open in dev and test. */
export function simulationEnabled(): boolean {
  return process.env.NODE_ENV !== 'production';
}

/**
 * Statuses worth simulating. Anything in the 4xx/5xx range is allowed so a
 * new category can be exercised without editing this list; the bound is what
 * keeps `?status=200` from producing a "successful failure".
 */
export function isSimulatableStatus(value: number): boolean {
  return Number.isInteger(value) && value >= 400 && value <= 599;
}

/**
 * `network` simulates a request that never got a response at all — a refused
 * socket. It is the one case with no HTTP status, and the only way to see the
 * page's "Error / Connection unavailable" state.
 */
export const NETWORK_KEYWORD = 'network';

export type Simulation = { kind: 'status'; status: number } | { kind: 'network' };

/** Parse a `status` parameter: "503" → status, "network" → transport failure. */
export function parseSimulation(raw: string | null): Simulation | null {
  const value = (raw ?? '').trim().toLowerCase();
  if (!value) return null;
  if (value === NETWORK_KEYWORD) return { kind: 'network' };
  const status = Number(value);
  return isSimulatableStatus(status) ? { kind: 'status', status } : null;
}

/**
 * The composer trigger.
 *
 * Typing `/simulate 503` sends an ordinary chat message, which means the
 * failure travels the whole real route — proxy → classification → error page
 * — and Retry and Return to chat behave exactly as they do for a genuine
 * outage. Visiting the debug URL directly only ever shows JSON; this is what
 * makes the PAGE testable by hand.
 */
const SIMULATE_COMMAND = /^\s*\/simulate\s+(\d{3}|network)\s*$/i;

export function parseSimulateCommand(message: string): Simulation | null {
  const match = SIMULATE_COMMAND.exec(message ?? '');
  return match ? parseSimulation(match[1]) : null;
}
