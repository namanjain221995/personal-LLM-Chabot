/**
 * POST /api/chat — the frontend's SSE endpoint (§10 contract).
 *
 * MOCK_MODE=true  → stream a canned fixture (lib/fixtures.ts) with realistic
 *                   token pacing, so the UI works before any model exists.
 * MOCK_MODE else  → translate the body to the orchestrator's ChatRequest
 *                   shape ({message, session_id, image_base64} — see
 *                   lib/orchestrator.ts), POST it to ORCHESTRATOR_URL/chat
 *                   and pipe the upstream SSE stream through untouched.
 */

import { parseSimulateCommand, simulationEnabled } from '@/lib/devErrors';
import { categoryForStatus, type ErrorCategory } from '@/lib/errorTypes';
import { FIXTURES, MOCK_MODEL_IDS, pickFixtureEngine } from '@/lib/fixtures';
import {
  lastUserContent,
  toOrchestratorChatRequest,
  type ChatRequestBody,
} from '@/lib/orchestrator';
import { logProxyError, requestIdOf } from '@/lib/serverLog';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const SSE_HEADERS = {
  'Content-Type': 'text/event-stream; charset=utf-8',
  'Cache-Control': 'no-cache, no-transform',
  Connection: 'keep-alive',
  'X-Accel-Buffering': 'no',
} as const;

/** Walk undici's nested `cause` chain for the machine-readable error code. */
function causeCode(err: unknown): string | undefined {
  let cur: unknown = err;
  for (let depth = 0; cur && depth < 5; depth += 1) {
    const code = (cur as { code?: unknown }).code;
    if (typeof code === 'string') return code;
    cur = (cur as { cause?: unknown }).cause;
  }
  return undefined;
}

function isAbort(err: unknown): boolean {
  return (
    (err as { name?: unknown } | null)?.name === 'AbortError' ||
    causeCode(err) === 'ABORT_ERR'
  );
}

function isTimeout(err: unknown): boolean {
  const code = causeCode(err);
  return (
    code === 'UND_ERR_HEADERS_TIMEOUT' ||
    code === 'UND_ERR_BODY_TIMEOUT' ||
    code === 'UND_ERR_CONNECT_TIMEOUT' ||
    code === 'ETIMEDOUT'
  );
}

/**
 * The orchestrator's own account of the failure, for the SERVER LOG.
 *
 * This used to feed a user-facing banner, so it screened out anything that
 * looked like a stack trace. It does not any more — the browser is sent a
 * status and a category and nothing else — and a traceback is precisely what
 * an engineer reading the log wants. `sanitizeForLog` redacts credentials,
 * flattens newlines and caps the length on the way out, so the screening that
 * remains here is only about finding the string at all.
 */
async function upstreamMessage(upstream: Response): Promise<string> {
  const generic = `orchestrator responded with status ${upstream.status}`;
  try {
    const body = (await upstream.json()) as {
      message?: unknown;
      detail?: unknown;
    };
    const said = body.message ?? body.detail;
    if (typeof said === 'string' && said.trim()) return said.trim();
    if (said !== undefined) return JSON.stringify(said);
  } catch {
    // Not JSON (an intermediary's own error page) — the status still says
    // what happened, and the category is derived from it.
  }
  return generic;
}

function sseFrame(event: string, data: unknown): Uint8Array {
  return new TextEncoder().encode(
    `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`,
  );
}

/** Split text into small word-ish deltas for realistic streaming. */
function tokenize(text: string): string[] {
  return text.match(/\S+\s*|\s+/g) ?? [];
}

function mockStream(body: ChatRequestBody): Response {
  const lastUser = lastUserContent(body);
  const fixture =
    FIXTURES[
      pickFixtureEngine(lastUser, Boolean(body.image), {
        mode: body.mode,
        agent: body.agent,
      })
    ];
  const tokens = tokenize(fixture.text);
  const model = body.model === 'fast' ? 'fast' : 'smart';
  // Fast model (Qwen3-4B-Instruct) has no reasoning stream (V2 §3a).
  const reasoningTokens =
    model === 'smart' && fixture.reasoning ? tokenize(fixture.reasoning) : [];
  // The mock reports what it "served" (V2 §2 meta keys).
  const meta = {
    ...fixture.meta,
    mode: body.mode ?? fixture.meta.mode ?? 'salesforce',
    model: MOCK_MODEL_IDS[model],
    effort: body.effort ?? fixture.meta.effort ?? 'medium',
  };

  let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const sleep = (ms: number) =>
        new Promise((resolve) => setTimeout(resolve, ms));
      try {
        // An event type the frontend does not know — the stream must keep
        // working (V2 §2: unknown event types are ignored gracefully).
        controller.enqueue(sseFrame('ping', { ts: Date.now() }));
        // Pre-first-token pause: lets the UI shimmer be seen.
        await sleep(450);

        for (const delta of reasoningTokens) {
          if (cancelled) return;
          controller.enqueue(sseFrame('reasoning', { text: delta }));
          await sleep(Math.random() < 0.05 ? 90 : 8 + Math.random() * 16);
        }

        for (const step of fixture.steps ?? []) {
          if (cancelled) return;
          controller.enqueue(
            sseFrame('step', {
              id: step.id,
              title: step.title,
              status: 'running',
            }),
          );
          await sleep(500 + Math.random() * 500);
          if (cancelled) return;
          controller.enqueue(
            sseFrame('step', {
              id: step.id,
              title: step.title,
              status: step.status,
              ...(step.detail ? { detail: step.detail } : {}),
            }),
          );
        }

        for (const token of tokens) {
          if (cancelled) return;
          controller.enqueue(sseFrame('token', { text: token }));
          // Realistic pacing: mostly fast, occasional thought-pauses.
          await sleep(Math.random() < 0.06 ? 120 : 14 + Math.random() * 26);
        }
        if (cancelled) return;
        controller.enqueue(sseFrame('meta', meta));
        controller.enqueue(sseFrame('done', {}));
        controller.close();
      } catch {
        // Client disconnected mid-stream — nothing to clean up.
      }
    },
    cancel() {
      cancelled = true;
    },
  });

  return new Response(stream, { headers: SSE_HEADERS });
}

/**
 * One exit for every failed chat request: log the real cause server-side,
 * answer the browser with the STATUS and a category and nothing else.
 *
 * The body carries no upstream text on purpose. lib/errorTypes turns
 * {status, code} into the page's copy, so there is no route by which an
 * orchestrator sentence — or anything it quoted — can reach the DOM.
 */
function failure(
  req: Request,
  info: {
    status: number | null;
    category: ErrorCategory;
    logMessage?: string | null;
    exception?: string | null;
    startedAt: number;
    simulated?: boolean;
  },
): Response {
  logProxyError({
    route: '/api/chat',
    status: info.status,
    category: info.category,
    message: info.logMessage,
    requestId: requestIdOf(req),
    durationMs: Date.now() - info.startedAt,
    retryable: true,
    exception: info.exception,
    simulated: info.simulated,
  });
  return Response.json(
    { code: info.category },
    // A transport failure has no status of its own; 502 is what this proxy
    // reports for "I could not complete this upstream call", while `code`
    // carries the distinction the page actually renders.
    { status: info.status ?? 502 },
  );
}

/** A short, non-leaking description of a thrown transport error, for the log. */
function describeThrown(err: unknown): string {
  const code = causeCode(err);
  const name = (err as { name?: unknown })?.name;
  const msg = (err as { message?: unknown })?.message;
  return [
    typeof name === 'string' ? name : null,
    code ?? null,
    typeof msg === 'string' ? msg : null,
  ]
    .filter(Boolean)
    .join(': ');
}

export async function POST(req: Request): Promise<Response> {
  const startedAt = Date.now();
  let body: ChatRequestBody;
  try {
    body = (await req.json()) as ChatRequestBody;
  } catch {
    return failure(req, {
      status: 400,
      category: 'APPLICATION_ERROR',
      logMessage: 'request body was not JSON',
      startedAt,
    });
  }

  // DEV ONLY: "/simulate 503" in the composer fails the send with that
  // status, so the error page can be exercised by hand without breaking a
  // service. Checked before MOCK_MODE so it works in either mode, and before
  // any outbound call — a simulated failure never touches the orchestrator.
  if (simulationEnabled()) {
    const simulation = parseSimulateCommand(lastUserContent(body));
    if (simulation) {
      const status = simulation.kind === 'network' ? null : simulation.status;
      return failure(req, {
        status,
        category:
          simulation.kind === 'network'
            ? 'NETWORK_ERROR'
            : categoryForStatus(status),
        logMessage: 'SIMULATED_ERROR requested from the composer',
        startedAt,
        simulated: true,
      });
    }
  }

  if (process.env.MOCK_MODE === 'true') {
    return mockStream(body);
  }

  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';

  // The orchestrator's ChatRequest takes a single non-empty `message`
  // (plus session_id / image_base64), not the UI's messages array —
  // translate before forwarding (§10).
  const chatRequest = toOrchestratorChatRequest(body);
  if (!chatRequest) {
    return failure(req, {
      status: 400,
      category: 'APPLICATION_ERROR',
      logMessage: 'no user message or image in request',
      startedAt,
    });
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${orchestratorUrl}/chat`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        // V9: forward the session cookie so the orchestrator can identify the
        // signed-in user and pull in cross-chat memory.
        ...(req.headers.get('cookie')
          ? { cookie: req.headers.get('cookie') as string }
          : {}),
      },
      body: JSON.stringify(chatRequest),
      signal: req.signal,
    });
  } catch (err) {
    // The browser navigated away or hit Stop: the fetch it was reading is
    // already gone, so there is nobody left to read a body.
    if (isAbort(err)) return new Response(null, { status: 499 });
    // "Unreachable" must mean unreachable. undici reports the real cause in a
    // nested chain: a refused/unresolvable host is a down service, while a
    // timeout means the orchestrator DID answer the socket and then went
    // quiet — a different problem, and a different thing for the user to do.
    const timedOut = isTimeout(err);
    return failure(req, {
      // A refused socket has no HTTP status and must not be given a fake one:
      // the page says "Error / Connection unavailable" rather than inventing
      // a number the service never sent.
      status: timedOut ? 504 : null,
      category: timedOut ? 'TIMEOUT' : 'NETWORK_ERROR',
      logMessage: describeThrown(err),
      exception: causeCode(err) ?? (err as { name?: string })?.name ?? null,
      startedAt,
    });
  }

  if (!upstream.ok || !upstream.body) {
    // The orchestrator's OWN sentence is the only account of whether the model
    // is loading, out of memory or over its context window — so it is kept,
    // and it is written to the SERVER log. It is not sent to the browser: the
    // user gets the status and the safe copy that goes with it.
    return failure(req, {
      status: upstream.status,
      category: categoryForStatus(upstream.status),
      logMessage: await upstreamMessage(upstream),
      startedAt,
    });
  }

  // Pipe the SSE stream through untouched.
  return new Response(upstream.body, { headers: SSE_HEADERS });
}
