/**
 * Which /api/history/* paths the proxy will forward.
 *
 * Extracted from the route handler so it can be tested without Next: the
 * allowlist living only inside a route handler is how a real bug shipped —
 * per-message feedback (`conversations/<id>/messages/<mid>/feedback`) is FIVE
 * segments, the cap was three, so the browser got a 404 from its own frontend
 * and the request never reached the orchestrator. The store had tests, the
 * endpoint had tests, and the layer between them had none.
 *
 * It stays an ALLOWLIST rather than a depth cap: raising the limit to five
 * would forward any invented five-segment URL to the orchestrator.
 */

export type HistoryProxyDecision =
  | { kind: 'search' }
  | { kind: 'conversations' }
  | { kind: 'message-feedback' }
  | { kind: 'reject' };

export function classifyHistoryPath(
  parts: readonly string[],
  method: string,
): HistoryProxyDecision {
  // Read-only chat search (V4 §2), GET only.
  if (parts.length === 1 && parts[0] === 'search') {
    return method === 'GET' ? { kind: 'search' } : { kind: 'reject' };
  }

  // conversations/<id>/messages/<messageId>/feedback — the one deep path.
  if (
    parts.length === 5 &&
    parts[0] === 'conversations' &&
    parts[2] === 'messages' &&
    parts[4] === 'feedback'
  ) {
    return method === 'PUT'
      ? { kind: 'message-feedback' }
      : { kind: 'reject' };
  }

  // The documented conversations tree: conversations[/<id>[/<sub>]].
  if (parts[0] === 'conversations' && parts.length <= 3) {
    return { kind: 'conversations' };
  }

  return { kind: 'reject' };
}
