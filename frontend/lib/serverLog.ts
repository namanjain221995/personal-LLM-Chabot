/**
 * Structured server-side logging for the Next.js route handlers.
 *
 * The UI deliberately shows a user nothing but a status and two friendly
 * sentences, which leaves engineers with no way to tell a refused socket from
 * a model that fell over — so the detail that used to be dumped into the chat
 * thread is written HERE instead, where it is useful and where the person
 * reading it is entitled to it.
 *
 * One line per event, `key=value`, so it greps and it ships. Everything that
 * came from another service goes through `sanitizeForLog` first: an upstream
 * sentence can carry a DSN password or an echoed Authorization header, and a
 * log line is exactly the kind of thing that ends up pasted into a ticket.
 *
 * Server-only: route handlers run in Node, and nothing in the browser bundle
 * imports this.
 */
import { sanitizeForLog, type ErrorCategory } from './errorTypes';

export interface ProxyErrorLog {
  route: string;
  /** Real HTTP status, or null when the request never got one. */
  status: number | null;
  category: ErrorCategory;
  /** Upstream's own sentence. Sanitized and capped before it is written. */
  message?: string | null;
  requestId?: string | null;
  durationMs?: number;
  retryable?: boolean;
  /** Exception constructor / undici code, when the failure threw. */
  exception?: string | null;
  /**
   * True for a failure produced by the dev-only simulator. Marked loudly so
   * nobody chases a deliberately-broken request through the logs, and so a
   * simulated line can never be mistaken for a real outage in a paste.
   */
  simulated?: boolean;
}

function field(key: string, value: string | number | boolean): string {
  return typeof value === 'string' ? `${key}="${value}"` : `${key}=${value}`;
}

/** Render one log line. Exported for tests — assert on this, not on stderr. */
export function formatProxyError(entry: ProxyErrorLog): string {
  const parts: string[] = [
    field('timestamp', new Date().toISOString()),
    field('route', entry.route),
    field('status', entry.status ?? 'none'),
    field('category', entry.category),
  ];
  if (entry.requestId) parts.push(field('request_id', sanitizeForLog(entry.requestId)));
  if (entry.exception) parts.push(field('exception', sanitizeForLog(entry.exception)));
  if (typeof entry.durationMs === 'number') {
    parts.push(field('duration_ms', Math.round(entry.durationMs)));
  }
  if (typeof entry.retryable === 'boolean') {
    parts.push(field('retryable', entry.retryable));
  }
  // Both a leading tag (for skimming) and a field (for parsing): a log
  // pipeline should be able to drop these without regex-matching a prefix.
  if (entry.simulated) parts.push(field('simulated', true));
  const message = sanitizeForLog(entry.message);
  if (message) parts.push(field('message', message));
  // The tag goes in front so `grep SIMULATED_ERROR` and `grep -v` both work
  // on whole lines, and so it is impossible to miss when skimming.
  const tag = entry.simulated
    ? '[chat-proxy:error] SIMULATED_ERROR'
    : '[chat-proxy:error]';
  return `${tag} ${parts.join(' ')}`;
}

export function logProxyError(entry: ProxyErrorLog): void {
  // console.error is what the Next.js server writes to stderr; there is no
  // other logger in this app to route through.
  console.error(formatProxyError(entry));
}

/**
 * A correlation id for one request, preferring one an upstream proxy already
 * assigned so the two sides of a trace agree. Returns null rather than
 * inventing one when nothing supplied it — a fabricated id that appears in no
 * other system is worse than an absent field.
 */
export function requestIdOf(req: Request): string | null {
  for (const header of ['x-request-id', 'x-correlation-id', 'x-amzn-trace-id']) {
    const value = req.headers.get(header);
    if (value && value.length <= 200) return value;
  }
  return null;
}
