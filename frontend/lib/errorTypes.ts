/**
 * The one place a failure becomes something a user is allowed to see.
 *
 * Two rules define this module.
 *
 * 1. **The status is a fact, and facts survive.** The chat proxy used to
 *    collapse every upstream status onto 502/503, and the client then decided
 *    what had happened by running a regex over the error SENTENCE. A real 404,
 *    a backend 500 and a model timeout all arrived as "the orchestrator is
 *    unreachable", which is the wrong thing to tell a user and the wrong thing
 *    to page an engineer about. Classification here keys off the status code
 *    and nothing else.
 *
 * 2. **What the UI receives is already safe.** `ClientError` carries a status,
 *    a category, and two sentences written for a person. It has no field for
 *    an upstream body, a URL, a request id or an exception type, so there is
 *    nothing for a component to leak even by accident. Everything an engineer
 *    needs is logged server-side instead (see serverLog.ts).
 *
 * Pure module: no imports, no I/O, safe on both sides of the network.
 */

/** What went wrong, at the coarseness a user-facing decision needs. */
export type ErrorCategory =
  | 'NOT_FOUND'
  | 'ORCHESTRATOR_UNAVAILABLE'
  | 'MODEL_UNAVAILABLE'
  | 'TIMEOUT'
  | 'APPLICATION_ERROR'
  | 'NETWORK_ERROR'
  | 'UNKNOWN_ERROR';

/**
 * The ONLY error shape a component may render. Deliberately minimal — if a
 * field cannot appear on a 404 page, it does not belong here.
 */
export interface ClientError {
  /** The real HTTP status, or null when the request never got one. */
  status: number | null;
  code: ErrorCategory;
  /** Large heading — the code, or "Error" when there is no honest number. */
  display: string;
  title: string;
  message: string;
  retryable: boolean;
}

interface Copy {
  title: string;
  message: string;
  retryable: boolean;
}

/**
 * Public copy per category. Written to be true of every failure that maps
 * here: no category may promise a cause it cannot know.
 */
const COPY: Record<ErrorCategory, Copy> = {
  NOT_FOUND: {
    title: "We couldn't find the page",
    message: "The page or resource you're looking for doesn't exist.",
    // A missing resource does not become present on a second try, but the
    // button costs nothing and a stale link is the common case.
    retryable: true,
  },
  ORCHESTRATOR_UNAVAILABLE: {
    title: 'AI service unavailable',
    message:
      "We couldn't complete your request because the AI service is " +
      'temporarily unavailable. Please try again.',
    retryable: true,
  },
  MODEL_UNAVAILABLE: {
    title: 'Model server unavailable',
    message:
      'The model is temporarily unavailable. It may still be starting up — ' +
      'please try again in a moment.',
    retryable: true,
  },
  TIMEOUT: {
    title: 'Request timed out',
    message: 'The request took too long to complete. Please try again.',
    retryable: true,
  },
  APPLICATION_ERROR: {
    title: 'Something went wrong',
    message:
      "We couldn't complete your request. The issue has been logged. " +
      'Please try again.',
    retryable: true,
  },
  NETWORK_ERROR: {
    title: 'Connection unavailable',
    message:
      "We couldn't reach the service. Check your connection and try again.",
    retryable: true,
  },
  UNKNOWN_ERROR: {
    title: 'Something went wrong',
    message: "We couldn't complete your request. Please try again.",
    retryable: true,
  },
};

/**
 * Status → category. The gateway family is split deliberately: 502/504 are
 * reported by whatever sits in FRONT of the model, so they describe the model
 * path, while 503 is the orchestrator itself saying it cannot serve.
 */
export function categoryForStatus(status: number | null): ErrorCategory {
  if (status === null) return 'NETWORK_ERROR';
  if (status === 404) return 'NOT_FOUND';
  if (status === 408 || status === 504) return 'TIMEOUT';
  if (status === 502) return 'MODEL_UNAVAILABLE';
  if (status === 503) return 'ORCHESTRATOR_UNAVAILABLE';
  if (status >= 500) return 'APPLICATION_ERROR';
  // A 4xx that reached here is the app asking for something the orchestrator
  // rejected — not the user's connection, and not the model's fault.
  if (status >= 400) return 'APPLICATION_ERROR';
  return 'UNKNOWN_ERROR';
}

/** The category a category-name string denotes, or null if unrecognized. */
export function parseCategory(value: unknown): ErrorCategory | null {
  return typeof value === 'string' && value in COPY
    ? (value as ErrorCategory)
    : null;
}

/**
 * Build the renderable error.
 *
 * `code` wins when supplied and valid — the proxy knows things the status
 * cannot express, such as "the socket was refused" (no status at all) versus
 * "the orchestrator answered 502". Otherwise the status decides.
 */
export function toClientError(
  status: number | null,
  code?: unknown,
): ClientError {
  const category = parseCategory(code) ?? categoryForStatus(status);
  const copy = COPY[category];
  return {
    status,
    code: category,
    // Never show a number we did not actually receive: a model timeout
    // labelled "404" is a lie, and the point of this page is that it isn't.
    display: status === null ? 'Error' : String(status),
    title: copy.title,
    message: copy.message,
    retryable: copy.retryable,
  };
}

/** The public copy for a category, without needing a status. */
export function copyForCategory(category: ErrorCategory): Copy {
  return COPY[category];
}

// --- log sanitization -------------------------------------------------------

/**
 * Redact credentials before anything is written to a log line.
 *
 * The upstream sentence is written by another service and can contain
 * anything it was handed: a DSN with a password, an Authorization header it
 * echoed back, a token in a query string. Logs get shipped, pasted into
 * tickets and read by people who should not have those values, so the
 * redaction happens at the boundary rather than being left to callers.
 */
const REDACTIONS: Array<[RegExp, string]> = [
  // Bearer / token / api-key style headers and assignments.
  [/\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}/gi, '$1 [redacted]'],
  [
    /\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)\b\s*[:=]\s*[^\s,;]+/gi,
    '$1=[redacted]',
  ],
  [
    /\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|secret|client[_-]?secret|consumer[_-]?secret|password|passwd|pwd|hf[_-]?token|session[_-]?secret)\b\s*[:=]\s*["']?[^\s"',;)]+/gi,
    '$1=[redacted]',
  ],
  // Credentials embedded in a URL: postgres://user:pw@host, https://u:p@h.
  [/\b([a-z][a-z0-9+.-]*:\/\/)[^\s/@:]+:[^\s/@]+@/gi, '$1[redacted]@'],
  // Bare high-entropy tokens with a known prefix.
  [/\bhf_[A-Za-z0-9]{8,}/g, 'hf_[redacted]'],
  [/\bsk-[A-Za-z0-9._-]{8,}/g, 'sk-[redacted]'],
  [/\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}/g, '[redacted-jwt]'],
];

/** Max characters of upstream text kept in a log line. */
export const LOG_MESSAGE_CAP = 300;

export function sanitizeForLog(raw: unknown): string {
  if (typeof raw !== 'string') return '';
  let out = raw;
  for (const [pattern, replacement] of REDACTIONS) {
    out = out.replace(pattern, replacement);
  }
  // Collapse newlines: a multi-line traceback would otherwise break the
  // one-line-per-event format every log reader here assumes.
  out = out.replace(/\s+/g, ' ').trim();
  return out.length > LOG_MESSAGE_CAP
    ? `${out.slice(0, LOG_MESSAGE_CAP)}…`
    : out;
}
