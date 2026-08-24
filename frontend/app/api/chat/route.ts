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

import { FIXTURES, MOCK_MODEL_IDS, pickFixtureEngine } from '@/lib/fixtures';
import {
  lastUserContent,
  toOrchestratorChatRequest,
  type ChatRequestBody,
} from '@/lib/orchestrator';

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
 * The orchestrator's own error sentence, or a safe generic one. Bounded and
 * trace-screened: a user-facing banner must never become a stack dump.
 */
async function upstreamMessage(upstream: Response): Promise<string> {
  const generic = `The orchestrator responded with status ${upstream.status}.`;
  try {
    const body = (await upstream.json()) as {
      message?: unknown;
      detail?: unknown;
    };
    const said = body.message ?? body.detail;
    if (typeof said === 'string') {
      const trimmed = said.trim();
      if (trimmed && trimmed.length <= 300 && !/traceback|\bat \w+ \(/i.test(trimmed)) {
        return trimmed;
      }
    }
  } catch {
    // Not JSON (an intermediary's own error page) — use the generic sentence.
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

export async function POST(req: Request): Promise<Response> {
  let body: ChatRequestBody;
  try {
    body = (await req.json()) as ChatRequestBody;
  } catch {
    return Response.json(
      { message: 'Request body must be JSON with a messages array.' },
      { status: 400 },
    );
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
    return Response.json(
      { message: 'The request contains no user message or image to send.' },
      { status: 400 },
    );
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
    if (isTimeout(err)) {
      return Response.json(
        { message: 'The orchestrator stopped responding before the answer started.' },
        { status: 504 },
      );
    }
    return Response.json(
      { message: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }

  if (!upstream.ok || !upstream.body) {
    // Forward the orchestrator's OWN sentence when it sent one: it is the only
    // party that knows whether the model is loading, out of memory, or over its
    // context window. "responded with status 502" told the user none of that.
    return Response.json(
      { message: await upstreamMessage(upstream) },
      { status: upstream.status === 503 ? 503 : 502 },
    );
  }

  // Pipe the SSE stream through untouched.
  return new Response(upstream.body, { headers: SSE_HEADERS });
}
