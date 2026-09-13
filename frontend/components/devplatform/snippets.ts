/**
 * "Copy as cURL / Python / JavaScript" for the playground.
 *
 * THE ONE RULE: no snippet may ever contain a key. Every one of these reads
 * the key out of the environment, because a snippet that carries a secret is
 * pasted into a terminal, a chat, a ticket and eventually a public repository
 * — and CONTRACT §5 is that the secret exists exactly once, at creation, and
 * is never shown again anywhere in this product. The playground itself never
 * holds a key either (it runs on the session, through the console BFF), so
 * there is no key here to leak even by accident: the placeholder is the only
 * thing these functions know.
 *
 * Pure functions with no React in sight, so the shapes can be asserted
 * character by character.
 */

/** The environment variable every snippet reads the key from. */
export const KEY_ENV_VAR = 'TECHSARA_API_KEY';

export interface SnippetRequest {
  /** Origin only, no trailing slash — the page passes window.location.origin. */
  baseUrl: string;
  model: string;
  input: string;
  instructions?: string;
  stream?: boolean;
  temperature?: number | null;
  maxOutputTokens?: number | null;
}

/**
 * The request body, built once so all three snippets agree.
 *
 * A field the person did not set is ABSENT rather than null: CONTRACT §8
 * refuses a parameter it cannot honour, and a snippet that sends
 * `"temperature": null` teaches a reader to send a field they did not mean.
 */
export function snippetBody(req: SnippetRequest): Record<string, unknown> {
  const body: Record<string, unknown> = {
    model: req.model,
    input: req.input,
  };
  if (req.instructions && req.instructions.trim()) {
    body.instructions = req.instructions;
  }
  if (req.stream) body.stream = true;
  if (typeof req.temperature === 'number') body.temperature = req.temperature;
  if (typeof req.maxOutputTokens === 'number') {
    body.max_output_tokens = req.maxOutputTokens;
  }
  return body;
}

function endpoint(baseUrl: string): string {
  return `${baseUrl.replace(/\/+$/, '')}/v1/responses`;
}

/**
 * Quote a string for a POSIX shell.
 *
 * Single quotes, with the one escape the shell allows: a literal `'` has to
 * close the quoting, emit an escaped quote and reopen it. Without this a
 * prompt containing an apostrophe — which is most English prompts — produces
 * a snippet that does not run.
 */
export function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

export function curlSnippet(req: SnippetRequest): string {
  const body = JSON.stringify(snippetBody(req), null, 2);
  return [
    // -N: print each event as it arrives instead of buffering the answer.
    `curl ${req.stream ? '-N ' : ''}${endpoint(req.baseUrl)} \\`,
    `  -H "Authorization: Bearer $${KEY_ENV_VAR}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d ${shellQuote(body)}`,
  ].join('\n');
}

/**
 * A Python literal for a JSON value, indented by four spaces a level.
 *
 * NOT `JSON.stringify` (fixed 2026-09-13): JSON spells `true`, `false` and
 * `null`, which are NameErrors in Python, so every Python snippet with
 * `"stream": true` failed before it sent a byte. Strings go through
 * `JSON.stringify`, whose escapes (`\"`, `\\`, `\n`, `\uXXXX`) are all valid
 * inside a Python double-quoted string.
 */
export function pythonLiteral(value: unknown, depth = 0): string {
  const pad = (level: number) => '    '.repeat(level);
  if (value === null || value === undefined) return 'None';
  if (value === true) return 'True';
  if (value === false) return 'False';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'None';
  if (typeof value === 'string') return JSON.stringify(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    const items = value.map((item) => `${pad(depth + 1)}${pythonLiteral(item, depth + 1)}`);
    return `[\n${items.join(',\n')}\n${pad(depth)}]`;
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return '{}';
  const lines = entries.map(
    ([key, item]) => `${pad(depth + 1)}${JSON.stringify(key)}: ${pythonLiteral(item, depth + 1)}`,
  );
  return `{\n${lines.join(',\n')}\n${pad(depth)}}`;
}

/**
 * A STREAMED request reads the event stream line by line (2026-09-13). The
 * snippet used to call `response.json()` on a `stream: true` body, which is
 * an event stream and not JSON, so the copied code failed on its first run —
 * and with output ceilings up to 1,000,000 tokens the stream is the only
 * shape a long answer can take. The read timeout is per chunk, not per
 * answer: the server sends a heartbeat every 15 s, so 60 s only trips on a
 * dead connection however long the generation runs.
 */
export function pythonSnippet(req: SnippetRequest): string {
  const body = pythonLiteral(snippetBody(req));
  const auth = `    headers={"Authorization": f"Bearer {os.environ['${KEY_ENV_VAR}']}"},`;
  if (req.stream) {
    const indented = body.split('\n').join('\n    ');
    return [
      'import os',
      'import httpx',
      '',
      'with httpx.stream(',
      '    "POST",',
      `    ${JSON.stringify(endpoint(req.baseUrl))},`,
      auth,
      `    json=${indented},`,
      '    timeout=httpx.Timeout(30.0, read=60.0),',
      ') as response:',
      '    response.raise_for_status()',
      '    for line in response.iter_lines():',
      '        if line.startswith("data: "):',
      '            print(line[len("data: "):])',
    ].join('\n');
  }
  return [
    'import os',
    'import httpx',
    '',
    `response = httpx.post(`,
    `    ${JSON.stringify(endpoint(req.baseUrl))},`,
    auth,
    `    json=${body},`,
    '    timeout=120.0,',
    ')',
    'response.raise_for_status()',
    'print(response.json())',
  ].join('\n');
}

export function javascriptSnippet(req: SnippetRequest): string {
  const body = JSON.stringify(snippetBody(req), null, 2);
  const request = [
    `const response = await fetch(${JSON.stringify(endpoint(req.baseUrl))}, {`,
    `  method: 'POST',`,
    '  headers: {',
    `    Authorization: \`Bearer \${process.env.${KEY_ENV_VAR}}\`,`,
    `    'Content-Type': 'application/json',`,
    '  },',
    `  body: JSON.stringify(${body}),`,
    '});',
    'if (!response.ok) throw new Error(`HTTP ${response.status}`);',
  ];
  if (req.stream) {
    return [
      ...request,
      'const decoder = new TextDecoder();',
      'for await (const chunk of response.body) {',
      '  process.stdout.write(decoder.decode(chunk, { stream: true }));',
      '}',
    ].join('\n');
  }
  return [...request, 'console.log(await response.json());'].join('\n');
}

export const SNIPPET_LANGUAGES = [
  { id: 'curl', label: 'cURL', build: curlSnippet },
  { id: 'python', label: 'Python', build: pythonSnippet },
  { id: 'javascript', label: 'JavaScript', build: javascriptSnippet },
] as const;

export type SnippetLanguage = (typeof SNIPPET_LANGUAGES)[number]['id'];
