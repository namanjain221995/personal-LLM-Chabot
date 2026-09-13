/**
 * The playground's "Use this from your code" snippets, RUN.
 *
 * A snippet is copied into a terminal, so a test that only reads its text can
 * pass while the code does not run — which is exactly how the first streaming
 * snippets shipped: they called `response.json()` on an event stream and threw
 * on their first line (responsive audit, 2026-09-13). Here each snippet is
 * executed as a person would run it, against a local stub server that speaks
 * the CONTRACT §10 stream the way `orchestrator/app/publicapi/events.py`
 * frames it: `event:` then `data:` lines, a `: ping` heartbeat comment, and
 * frames split across chunk boundaries mid-line.
 *
 * The stub checks the bearer header and the body the snippet sent, so a
 * snippet that reads the key from the wrong place or mangles its JSON fails
 * here too. Node always runs; curl and Python (httpx) run where installed.
 */
import { execFile, execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import {
  KEY_ENV_VAR,
  curlSnippet,
  javascriptSnippet,
  pythonSnippet,
  type SnippetRequest,
} from '@/components/devplatform/snippets';

const KEY = 'tsk_test_stubkeyforthesnippetrun';
const ANSWER = 'Retrieval-augmented generation looks things up first — it’s “grounded”.';

function frame(name: string, sequence: number, payload: Record<string, unknown>): string {
  return `event: ${name}\ndata: ${JSON.stringify({ type: name, sequence_number: sequence, ...payload })}\n\n`;
}

function responseObject(status: string, extra: Record<string, unknown> = {}) {
  return {
    id: 'resp_stub',
    object: 'response',
    created_at: 1789200000,
    status,
    model: 'techsara-35b',
    output: [],
    max_output_tokens: 512,
    incomplete_details: null,
    usage: null,
    ...extra,
  };
}

const seen: { authorization?: string; body?: Record<string, unknown> }[] = [];

async function handle(req: IncomingMessage, res: ServerResponse) {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  const body = JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown>;
  seen.push({ authorization: req.headers.authorization, body });
  if (req.url !== '/v1/responses' || req.headers.authorization !== `Bearer ${KEY}`) {
    res.writeHead(401, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: { code: 'invalid_api_key', message: 'No.' } }));
    return;
  }
  if (!body.stream) {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify(responseObject('completed', { output_text: ANSWER })));
    return;
  }
  res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
  const failing = body.input === 'fail please';
  const wire = [
    ': ping\n\n',
    frame('response.created', 1, { response: responseObject('in_progress') }),
    frame('response.in_progress', 2, { response: responseObject('in_progress') }),
  ];
  let sequence = 3;
  for (const piece of ANSWER.match(/.{1,9}/gu) ?? []) {
    wire.push(
      frame('response.output_text.delta', sequence++, {
        item_id: 'msg_stub',
        output_index: 0,
        content_index: 0,
        delta: piece,
      }),
    );
    if (sequence === 5) wire.push(': ping\n\n');
  }
  if (failing) {
    wire.push(
      frame('response.failed', sequence, {
        response: responseObject('failed', {
          error: { code: 'model_recovering', message: 'The model is restarting.' },
        }),
      }),
    );
  } else {
    wire.push(
      frame('response.output_text.done', sequence++, {
        item_id: 'msg_stub',
        output_index: 0,
        content_index: 0,
        text: ANSWER,
      }),
      frame('response.completed', sequence, {
        response: responseObject('completed', {
          usage: { input_tokens: 5, output_tokens: 17, total_tokens: 22 },
        }),
      }),
    );
  }
  // Split the wire into uneven chunks so frames — and multi-byte characters —
  // straddle chunk boundaries, as they do through a proxy.
  const bytes = Buffer.from(wire.join(''), 'utf8');
  for (let at = 0; at < bytes.length; at += 37) {
    res.write(bytes.subarray(at, at + 37));
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  res.end();
}

let server: Server;
let baseUrl = '';
let dir = '';

beforeAll(async () => {
  server = createServer((req, res) => {
    void handle(req, res);
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  dir = mkdtempSync(join(tmpdir(), 'techsara-snippets-'));
});

afterAll(async () => {
  await new Promise((resolve) => server.close(resolve));
  rmSync(dir, { recursive: true, force: true });
});

function request(overrides: Partial<SnippetRequest> = {}): SnippetRequest {
  return {
    baseUrl,
    model: 'techsara-35b',
    input: 'Explain RAG — it’s "grounded", isn\'t it?',
    instructions: 'Answer briefly.',
    stream: true,
    temperature: 0.2,
    maxOutputTokens: 512,
    ...overrides,
  };
}

function run(
  command: string,
  args: string[],
): Promise<{ code: number; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    execFile(
      command,
      args,
      { cwd: dir, env: { ...process.env, [KEY_ENV_VAR]: KEY }, timeout: 30_000 },
      (error, stdout, stderr) => {
        const code = error ? (typeof error.code === 'number' ? error.code : 1) : 0;
        resolve({ code, stdout: String(stdout), stderr: String(stderr) });
      },
    );
  });
}

function available(command: string, args: string[]): boolean {
  try {
    execFileSync(command, args, { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

const hasCurl = available('curl', ['--version']);
const hasHttpx = available('python3', ['-c', 'import httpx']);

describe('the JavaScript snippet, run with node against a stub stream', () => {
  it('prints the streamed answer and the usage, sending the key from the environment', async () => {
    seen.length = 0;
    const file = join(dir, 'request.mjs');
    writeFileSync(file, javascriptSnippet(request()));
    const { code, stdout, stderr } = await run(process.execPath, [file]);
    expect(stderr).toBe('');
    expect(code).toBe(0);
    expect(stdout).toContain(ANSWER);
    expect(stdout).toMatch(/usage: \{\s*input_tokens: 5, output_tokens: 17, total_tokens: 22\s*\}/);
    expect(seen[0]?.body).toMatchObject({ model: 'techsara-35b', stream: true, max_output_tokens: 512 });
    expect(seen[0]?.body?.input).toBe('Explain RAG — it’s "grounded", isn\'t it?');
  });

  it('exits non-zero with the error code when the stream ends in response.failed', async () => {
    const file = join(dir, 'failing.mjs');
    writeFileSync(file, javascriptSnippet(request({ input: 'fail please' })));
    const { code, stderr } = await run(process.execPath, [file]);
    expect(code).not.toBe(0);
    expect(stderr).toContain('model_recovering: The model is restarting.');
  });

  it('prints the JSON body of a request that does not stream', async () => {
    const file = join(dir, 'sync.mjs');
    writeFileSync(file, javascriptSnippet(request({ stream: false })));
    const { code, stdout } = await run(process.execPath, [file]);
    expect(code).toBe(0);
    expect(stdout).toContain("status: 'completed'");
  });
});

describe.skipIf(!hasHttpx)('the Python snippet, run with python3 and httpx against a stub stream', () => {
  it('prints the streamed answer and the usage', async () => {
    const file = join(dir, 'request.py');
    writeFileSync(file, pythonSnippet(request()));
    const { code, stdout, stderr } = await run('python3', [file]);
    expect(stderr).toBe('');
    expect(code).toBe(0);
    expect(stdout).toContain(ANSWER);
    expect(stdout).toContain("usage: {'input_tokens': 5, 'output_tokens': 17, 'total_tokens': 22}");
  });

  it('raises with the error code when the stream ends in response.failed', async () => {
    const file = join(dir, 'failing.py');
    writeFileSync(file, pythonSnippet(request({ input: 'fail please' })));
    const { code, stderr } = await run('python3', [file]);
    expect(code).not.toBe(0);
    expect(stderr).toContain('RuntimeError: model_recovering: The model is restarting.');
  });

  it('prints the JSON body of a request that does not stream', async () => {
    const file = join(dir, 'sync.py');
    writeFileSync(file, pythonSnippet(request({ stream: false })));
    const { code, stdout } = await run('python3', [file]);
    expect(code).toBe(0);
    expect(stdout).toContain("'status': 'completed'");
  });
});

describe.skipIf(!hasCurl)('the cURL snippet, run with bash against a stub stream', () => {
  it('sends the quoted body and prints every event as it arrives', async () => {
    seen.length = 0;
    const { code, stdout } = await run('bash', ['-c', curlSnippet(request())]);
    expect(code).toBe(0);
    expect(stdout).toContain('event: response.completed');
    expect(seen[0]?.body?.input).toBe('Explain RAG — it’s "grounded", isn\'t it?');
    expect(seen[0]?.authorization).toBe(`Bearer ${KEY}`);
  });
});
