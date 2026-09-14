// A scripted local HTTP server that speaks the CONTRACT-3 §9 envelope, for the
// offline tests of openai-node's own behaviour (retries, Retry-After, timeouts,
// SSE parsing). Nothing here talks to a TechSara stack.
import { createServer } from 'node:http';

/**
 * @param {(req: import('node:http').IncomingMessage, res: import('node:http').ServerResponse, n: number) => void|Promise<void>} handler
 *   called with the 0-based index of the request
 */
export async function startStub(handler) {
  const requests = [];
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const n = requests.length;
    requests.push({ at: Date.now(), method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString('utf8') });
    try {
      await handler(req, res, n);
    } catch (err) {
      if (!res.headersSent) res.writeHead(500);
      res.end(String(err));
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  return {
    baseURL: `http://127.0.0.1:${port}/v1`,
    requests,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections();
        server.close(resolve);
      }),
  };
}

export function sendError(res, status, code, type, { retryAfter, message = 'stub error', param = null } = {}) {
  const headers = { 'content-type': 'application/json', 'x-request-id': 'req_stub0000000000000000000000' };
  if (retryAfter !== undefined) headers['retry-after'] = String(retryAfter);
  res.writeHead(status, headers);
  res.end(JSON.stringify({ error: { message, type, code, param, request_id: 'req_stub0000000000000000000000' } }));
}

export function sendJSON(res, body, status = 200) {
  res.writeHead(status, { 'content-type': 'application/json', 'x-request-id': 'req_stub0000000000000000000000' });
  res.end(JSON.stringify(body));
}

export const MODEL_LIST = {
  object: 'list',
  data: [{ id: 'techsara-35b', object: 'model', owned_by: 'techsara', created: 0 }],
};

export function responseObject(status, text = '') {
  return {
    id: 'resp_stub',
    object: 'response',
    created_at: 1789200000,
    status,
    model: 'techsara-35b',
    output: text ? [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text }] }] : [],
    usage: status === 'completed' ? { input_tokens: 3, output_tokens: 1, total_tokens: 4 } : null,
  };
}
