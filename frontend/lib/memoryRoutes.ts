/**
 * Which /api/memory/* calls the proxy will forward (B11).
 *
 * An ALLOWLIST of exactly the three calls the memory panel makes, kept out of
 * the route handler so it can be tested without Next (the history proxy's
 * lesson: an allowlist that lives only inside a handler is how a five-segment
 * path shipped broken there). Everything not named here is a 404 that never
 * reaches the orchestrator — including POST /memory/facts, which exists
 * upstream for bulk imports but is not something the browser needs to reach.
 */

export type MemoryProxyDecision =
  | { kind: 'list' }
  | { kind: 'delete-one'; id: string }
  | { kind: 'clear-all' }
  | { kind: 'reject' };

/**
 * A fact id as the orchestrator issues it: a positive integer with no sign,
 * exponent, leading zero or padding. Capped at 18 digits so the value always
 * fits the BIGINT it is compared against; a longer one is not an id anyone
 * holds.
 */
const FACT_ID = /^[1-9][0-9]{0,17}$/;

export function classifyMemoryPath(
  parts: readonly string[],
  method: string,
  search: URLSearchParams,
): MemoryProxyDecision {
  if (parts.length === 1 && parts[0] === 'facts') {
    if (method === 'GET') return { kind: 'list' };
    // The clear-all is reachable only WITH its confirmation. The orchestrator
    // refuses the bare DELETE too (422), but an irreversible call deserves a
    // lock on both sides of the network.
    if (method === 'DELETE' && search.get('confirm') === 'all') {
      return { kind: 'clear-all' };
    }
    return { kind: 'reject' };
  }

  if (
    parts.length === 2 &&
    parts[0] === 'facts' &&
    method === 'DELETE' &&
    FACT_ID.test(parts[1])
  ) {
    return { kind: 'delete-one', id: parts[1] };
  }

  return { kind: 'reject' };
}

/**
 * The orchestrator path for an accepted decision. Built from the decision,
 * never from the request, so nothing a caller appends — a query parameter, a
 * second `confirm`, an encoded segment — can ride along upstream.
 */
export function upstreamMemoryPath(
  decision: Exclude<MemoryProxyDecision, { kind: 'reject' }>,
): string {
  switch (decision.kind) {
    case 'list':
      return '/memory/facts';
    case 'delete-one':
      return `/memory/facts/${decision.id}`;
    case 'clear-all':
      return '/memory/facts?confirm=all';
  }
}
