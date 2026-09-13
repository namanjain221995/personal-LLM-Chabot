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
    `curl ${endpoint(req.baseUrl)} \\`,
    `  -H "Authorization: Bearer $${KEY_ENV_VAR}" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d ${shellQuote(body)}`,
  ].join('\n');
}

export function pythonSnippet(req: SnippetRequest): string {
  const body = JSON.stringify(snippetBody(req), null, 4)
    .split('\n')
    .join('\n');
  return [
    'import os',
    'import httpx',
    '',
    `response = httpx.post(`,
    `    ${JSON.stringify(endpoint(req.baseUrl))},`,
    `    headers={"Authorization": f"Bearer {os.environ['${KEY_ENV_VAR}']}"},`,
    `    json=${body},`,
    '    timeout=120.0,',
    ')',
    'response.raise_for_status()',
    'print(response.json())',
  ].join('\n');
}

export function javascriptSnippet(req: SnippetRequest): string {
  const body = JSON.stringify(snippetBody(req), null, 2);
  return [
    `const response = await fetch(${JSON.stringify(endpoint(req.baseUrl))}, {`,
    `  method: 'POST',`,
    '  headers: {',
    `    Authorization: \`Bearer \${process.env.${KEY_ENV_VAR}}\`,`,
    `    'Content-Type': 'application/json',`,
    '  },',
    `  body: JSON.stringify(${body}),`,
    '});',
    'if (!response.ok) throw new Error(`HTTP ${response.status}`);',
    'console.log(await response.json());',
  ].join('\n');
}

export const SNIPPET_LANGUAGES = [
  { id: 'curl', label: 'cURL', build: curlSnippet },
  { id: 'python', label: 'Python', build: pythonSnippet },
  { id: 'javascript', label: 'JavaScript', build: javascriptSnippet },
] as const;

export type SnippetLanguage = (typeof SNIPPET_LANGUAGES)[number]['id'];
