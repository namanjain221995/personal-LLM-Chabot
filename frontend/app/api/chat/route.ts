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
  } catch {
    return Response.json(
      { message: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }

  if (!upstream.ok || !upstream.body) {
    return Response.json(
      {
        message: `The orchestrator responded with status ${upstream.status}.`,
      },
      { status: 502 },
    );
  }

  // Pipe the SSE stream through untouched.
  return new Response(upstream.body, { headers: SSE_HEADERS });
}
