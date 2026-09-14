import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EMBED_MODEL_ID,
  EXAMPLE_STATUS,
  MODEL_ID,
  RERANK_MODEL_ID,
  VISION_MODEL_ID,
  WHISPER_MODEL_ID,
} from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): the exact `openai` Node package
// settings (timeout 2147483647 — never 0 or Infinity — and the 7.5 whole-call
// deadline), retries that join a running request, and the resume request.
// Built in either state from NO_TIMEOUT_LIVE.

const INTRO = `
Everything here is plain \`fetch\`, so it runs on Node 18+, Deno, Bun and any
modern runtime. There is no TechSara SDK yet.
`;
const S_KEEP_THE_KEY_ON_THE_SERVER = `
## Keep the key on the server

~~~typescript
// NEVER do this. A key in browser code is a key published to everyone who
// opens developer tools, and it is in your bundle, your source maps and your
// CDN's cache.
const response = await fetch("${API_BASE_URL}/responses", {
  headers: { Authorization: \`Bearer \${"tsk_live_…"}\` },
});
~~~

Put the key in a server route of your own and let the browser talk to that.
Your route decides what the page is allowed to ask for; the key never leaves
your infrastructure. If you genuinely must call \`/v1\` from a browser, set an
origin allowlist on the project — see
[authentication](/docs/authentication#calling-from-a-browser).
`;
const S_TYPES = `
## Types

~~~typescript
export interface Usage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface OutputText {
  type: "output_text";
  text: string;
}

export interface OutputMessage {
  type: "message";
  role: "assistant";
  content: OutputText[];
}

export type ResponseStatus =
  | "queued" | "in_progress" | "completed" | "failed" | "cancelled";

export interface TechSaraResponse {
  id: string;
  object: "response";
  created_at: number;
  status: ResponseStatus;
  model: string;
  output: OutputMessage[];
  /** The output ceiling applied to this generation, after clamping. */
  max_output_tokens: number | null;
  /** Set when the answer stopped because it reached max_output_tokens. */
  incomplete_details: { reason: "max_output_tokens" } | null;
  /** null — never 0 — when the engine reported no counts. */
  usage: Usage | null;
  error?: { code: string; message: string };
}

export interface ApiErrorBody {
  error: {
    message: string;
    type: string;
    code: string;
    param: string | null;
    request_id: string;
  };
}
~~~
`;
const S_ONE_ANSWER = `
## One answer

~~~typescript
const BASE_URL = "${API_BASE_URL}";
const MODEL = "${MODEL_ID}";

export class TechSaraError extends Error {
  constructor(
    readonly code: string,
    readonly requestId: string,
    message: string,
  ) {
    super(message);
    this.name = "TechSaraError";
  }
}

export async function ask(question: string): Promise<string> {
  const response = await fetch(\`\${BASE_URL}/responses\`, {
    method: "POST",
    headers: {
      Authorization: \`Bearer \${process.env.TECHSARA_API_KEY}\`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ model: MODEL, input: question }),
  });

  if (!response.ok) {
    const { error } = (await response.json()) as ApiErrorBody;
    throw new TechSaraError(error.code, error.request_id, error.message);
  }

  const body = (await response.json()) as TechSaraResponse;
  return body.output
    .flatMap((item) => item.content)
    .filter((part) => part.type === "output_text")
    .map((part) => part.text)
    .join("");
}
~~~
`;
const S_STREAMING = `
## Streaming

~~~typescript
export async function* streamText(question: string): AsyncGenerator<string> {
  const response = await fetch(\`\${BASE_URL}/responses\`, {
    method: "POST",
    headers: {
      Authorization: \`Bearer \${process.env.TECHSARA_API_KEY}\`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ model: MODEL, input: question, stream: true }),
  });

  if (!response.ok || !response.body) {
    throw new Error(\`stream did not start: \${response.status}\`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let expected = 1;

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const frames = buffer.split("\\n\\n");
      buffer = frames.pop() ?? "";        // the tail may be half a frame

      for (const frame of frames) {
        let name = "";
        let data = "";
        for (const line of frame.split("\\n")) {
          if (line.startsWith(":")) continue;            // heartbeat comment
          if (line.startsWith("event: ")) name = line.slice(7);
          else if (line.startsWith("data: ")) data += line.slice(6);
        }
        if (!data) continue;

        const payload = JSON.parse(data);
        // Gaps mean a dropped or reordered frame; the numbering exists so a
        // client can notice rather than silently lose a sentence.
        if (payload.sequence_number !== expected) {
          throw new Error(\`sequence gap: expected \${expected}, got \${payload.sequence_number}\`);
        }
        expected += 1;

        if (name === "response.output_text.delta") yield payload.delta as string;
        if (name === "response.failed") {
          throw new Error(payload.response?.error?.code ?? "stream failed");
        }
        if (name === "error") {
          throw new Error(payload.code ?? "stream failed");
        }
      }
    }
  } finally {
    // Always release the connection, including on an early return from the
    // consumer — an abandoned reader holds the socket open.
    await reader.cancel().catch(() => undefined);
  }
}
~~~

Use it from a Next.js route handler and pipe it onward to your page; that way
the browser gets tokens as they are generated and the key stays on the
server.
`;
const S_RETRIES = `
## Retries

~~~typescript
const RETRYABLE = new Set([
  // quota_exceeded and concurrency_limit_exceeded arrive only if an operator
  // enables limits; listing them costs nothing.
  "rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded",
  "model_recovering", "model_unavailable", "timeout",
]);

export async function postWithRetry(
  path: string,
  payload: unknown,
  attempts = 5,
): Promise<unknown> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const response = await fetch(\`\${BASE_URL}\${path}\`, {
      method: "POST",
      headers: {
        Authorization: \`Bearer \${process.env.TECHSARA_API_KEY}\`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
    if (response.ok) return response.json();

    const { error } = (await response.json()) as ApiErrorBody;
    // A 409 with Retry-After is this key's first request, still running.
    const running = response.status === 409 && response.headers.has("Retry-After");
    if ((!RETRYABLE.has(error.code) && !running) || attempt === attempts - 1) {
      throw new TechSaraError(error.code, error.request_id, error.message);
    }
    const after = Number(response.headers.get("Retry-After") ?? 2 ** attempt);
    // Jitter: a fleet retrying on the same tick recreates the spike.
    await new Promise((r) => setTimeout(r, after * 1000 + Math.random() * 1000));
  }
  throw new Error("unreachable");
}
~~~
`;
const S_IDEMPOTENCY = `
## Idempotency

~~~typescript
await fetch(\`\${BASE_URL}/responses\`, {
  method: "POST",
  headers: {
    Authorization: \`Bearer \${process.env.TECHSARA_API_KEY}\`,
    "Content-Type": "application/json",
    // Per logical operation, not per attempt: every retry sends this same
    // value, which is what makes the retry safe.
    "Idempotency-Key": ticketId,
  },
  body: JSON.stringify({ model: MODEL, input: text, background: true }),
});
~~~

See [idempotency](/docs/idempotency), [background
responses](/docs/background) and [webhooks](/docs/webhooks), which has a
Node verification example.
`;
const S_IMAGES = `
## Images

~~~typescript
import { readFile } from "node:fs/promises";

export async function describe(path: string, question: string, mime = "image/png") {
  const dataUrl = \`data:\${mime};base64,\${(await readFile(path)).toString("base64")}\`;
  const response = await fetch(\`\${BASE_URL}/responses\`, {
    method: "POST",
    headers: {
      Authorization: \`Bearer \${process.env.TECHSARA_API_KEY}\`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: "${VISION_MODEL_ID}",
      input: [{
        role: "user",
        content: [
          { type: "input_text", text: question },
          { type: "input_image", image_url: dataUrl },
        ],
      }],
    }),
  });
  if (!response.ok) throw new Error(\`describe failed: \${response.status}\`);
  const body = (await response.json()) as TechSaraResponse;
  return body.output[0]?.content[0]?.text ?? "";
}
~~~

The image travels inside the request as a \`data:\` URL; a link is refused. See
[images and OCR](/docs/images).
`;
const S_EMBEDDINGS_AND_RERANK = `
## Embeddings and rerank

~~~typescript
async function post(path: string, payload: unknown) {
  const response = await fetch(\`\${BASE_URL}\${path}\`, {
    method: "POST",
    headers: {
      Authorization: \`Bearer \${process.env.TECHSARA_API_KEY}\`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(\`\${path} failed: \${response.status}\`);
  return response.json();
}

export async function embed(texts: string[]): Promise<number[][]> {
  const body = await post("/embeddings", { model: "${EMBED_MODEL_ID}", input: texts });
  return [...body.data]
    .sort((a: { index: number }, b: { index: number }) => a.index - b.index)
    .map((row: { embedding: number[] }) => row.embedding);
}

export async function rerank(query: string, documents: string[], topN = 3) {
  const body = await post("/rerank", { model: "${RERANK_MODEL_ID}", query, documents, top_n: topN });
  return body.results.map((r: { index: number; relevance_score: number }) => ({
    score: r.relevance_score,
    text: documents[r.index],
  }));
}
~~~
`;
const S_SPEECH_TO_TEXT = `
## Speech to text

~~~typescript
import { readFile } from "node:fs/promises";

export async function transcribe(path: string, mime = "audio/mp4"): Promise<string> {
  const form = new FormData();
  // The part's type must be an accepted audio type, so it is set explicitly.
  form.append("file", new Blob([await readFile(path)], { type: mime }), "clip");
  form.append("model", "${WHISPER_MODEL_ID}");

  const response = await fetch(\`\${BASE_URL}/audio/transcriptions\`, {
    method: "POST",
    // No Content-Type header: fetch writes the multipart boundary itself.
    headers: { Authorization: \`Bearer \${process.env.TECHSARA_API_KEY}\` },
    body: form,
  });
  if (!response.ok) throw new Error(\`transcription failed: \${response.status}\`);
  return ((await response.json()) as { text: string }).text;
}
~~~

See [embeddings](/docs/embeddings), [rerank](/docs/rerank) and
[audio transcriptions](/docs/audio-transcriptions).
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_RETRIES = `
## Retries

~~~typescript
const RETRYABLE = new Set([
  // quota_exceeded and concurrency_limit_exceeded arrive only if an operator
  // enables limits; listing them costs nothing.
  "rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded",
  "model_recovering", "model_unavailable",
]);

export async function postWithRetry(
  path: string,
  payload: unknown,
  idempotencyKey: string,
  giveUpAfterMs = 600_000,
): Promise<unknown> {
  const deadline = Date.now() + giveUpAfterMs;
  for (let attempt = 0; ; attempt += 1) {
    let response: Response | undefined;
    try {
      response = await fetch(\`\${BASE_URL}\${path}\`, {
        method: "POST",
        headers: {
          Authorization: \`Bearer \${process.env.TECHSARA_API_KEY}\`,
          "Content-Type": "application/json",
          // The same key on every attempt: a retry of a running request joins it.
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify(payload),
      });
    } catch {
      response = undefined;                       // dropped: retry below
    }
    if (response?.ok) {
      const body = (await response.json()) as TechSaraResponse;
      if (body.status === "failed") throw new Error(body.error?.code ?? "failed");
      return body;                                // a 200 can carry a failure
    }
    if (response && ![502, 524, 530].includes(response.status)) {
      const { error } = (await response.json()) as ApiErrorBody;
      if (response.headers.get("x-should-retry") === "false" || !RETRYABLE.has(error.code)) {
        throw new TechSaraError(error.code, error.request_id, error.message);
      }
    }
    if (Date.now() > deadline) throw new Error(\`gave up on \${path}\`);
    const after = Number(response?.headers.get("Retry-After") ?? 0) * 1000;
    // Jitter: a fleet retrying on the same tick recreates the spike.
    await new Promise((r) => setTimeout(r, Math.max(after, Math.min(60_000, 1000 * 2 ** attempt)) + Math.random() * 1000));
  }
}
~~~
`;
const LATER_SPEECH_TO_TEXT = `
## Speech to text

~~~typescript
import { readFile } from "node:fs/promises";

export async function transcribe(path: string, mime = "audio/mp4"): Promise<string> {
  const form = new FormData();
  // The part's type must be an accepted audio type, so it is set explicitly.
  form.append("file", new Blob([await readFile(path)], { type: mime }), "clip");
  form.append("model", "${WHISPER_MODEL_ID}");

  const response = await fetch(\`\${BASE_URL}/audio/transcriptions\`, {
    method: "POST",
    // No Content-Type header: fetch writes the multipart boundary itself.
    headers: { Authorization: \`Bearer \${process.env.TECHSARA_API_KEY}\` },
    body: form,
  });
  if (!response.ok) throw new Error(\`transcription failed: \${response.status}\`);
  return ((await response.json()) as { text: string }).text;
}
~~~

See [embeddings](/docs/embeddings), [rerank](/docs/rerank) and
[audio transcriptions](/docs/audio-transcriptions).

## With the \`openai\` package

The \`openai\` package speaks this API with a base URL and a key. Its timeout
decides whether a long request finishes:

~~~typescript
import OpenAI from "openai";
import { randomUUID } from "node:crypto";

const client = new OpenAI({
  baseURL: "${API_BASE_URL}",
  apiKey: process.env.TECHSARA_API_KEY,
  timeout: 2_147_483_647,
  maxRetries: 5,
});

const stream = await client.responses.create(
  { model: "${MODEL_ID}", input: "Explain retrieval-augmented generation.", stream: true },
  { headers: { "Idempotency-Key": randomUUID() } },
);
let lastSeq = 0;
for await (const event of stream) {
  lastSeq = event.sequence_number;
  if (event.type === "response.output_text.delta") process.stdout.write(event.delta);
}
~~~

* **\`timeout: 2_147_483_647\`** is the largest value the client honours; \`0\`
  and \`Infinity\` abort every call at once. From version 7.5 the timeout covers
  a whole non-streamed call, so with the default 600,000 a synchronous call
  longer than about half an hour fails in the SDK however healthy the request
  is. Stream, use background, or keep this maximum.
* **The \`Idempotency-Key\` goes per call**, on generations only: the
  embeddings, rerank and transcription endpoints refuse it.
* A broken stream is picked up with
  \`client.responses.retrieve(responseId, { stream: true, starting_after: lastSeq })\`
  — see [timeouts](/docs/timeouts#resuming-a-stream).
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function javascriptPage({ noTimeout }: { noTimeout: boolean }): DocPage {
  const sections = noTimeout
    ? [INTRO, S_KEEP_THE_KEY_ON_THE_SERVER, S_TYPES, S_ONE_ANSWER, S_STREAMING, LATER_RETRIES, S_IDEMPOTENCY, S_IMAGES, S_EMBEDDINGS_AND_RERANK, LATER_SPEECH_TO_TEXT]
    : [INTRO, S_KEEP_THE_KEY_ON_THE_SERVER, S_TYPES, S_ONE_ANSWER, S_STREAMING, S_RETRIES, S_IDEMPOTENCY, S_IMAGES, S_EMBEDDINGS_AND_RERANK, S_SPEECH_TO_TEXT];
  return {
    slug: 'javascript',
    title: 'JavaScript and TypeScript',
    summary:
      'fetch on the server, typed responses, streaming with a ReadableStream, ' +
      'and the one thing never to do in a browser.',
    section: 'Examples',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const javascript: DocPage = javascriptPage({ noTimeout: NO_TIMEOUT_LIVE });
