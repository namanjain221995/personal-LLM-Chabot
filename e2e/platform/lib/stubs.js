'use strict';
/**
 * Canned engine answers for the stub chat mode.
 *
 * WHY A STUB IS THE DEFAULT (2026-09-13): the isolated e2e stack deliberately
 * shares the production model engines (scripts/e2e-stack.sh), and a release
 * regression run must not put load on them. So by default the browser's own
 * POST /api/chat is answered in the browser with a stream in the exact wire
 * format the orchestrator uses (`event: token` / `meta` / `done`), and every
 * other call — history, sharing, uploads — still goes to the real stack.
 *
 * WHAT THE STUB DOES NOT COVER: the orchestrator's own persist of the answer
 * (the /chat worker writes it keyed by generation_id). With the stub, the
 * stored answer is the browser's history save. What the stub DOES carry since
 * the review (2026-09-13) is the real stream's handshake: the leading
 * `meta {generation_id, trace_id, request_id, intent_id, attempt}` before the
 * first token, and the same ids on the final meta (main.py, chat publish and
 * _replay_frames). Without it the browser never marks the send `accepted` and
 * never keys the answer by generation_id, which is the path production takes
 * and the one a duplicate-answer regression would break.
 * `--chat-mode live` sends for real.
 */

/** One SSE frame. `data` is JSON-encoded unless it is already a string. */
function sseFrame(event, data) {
  const payload = typeof data === 'string' ? data : JSON.stringify(data);
  const lines = payload.split('\n').map((l) => `data: ${l}`);
  return `${event ? `event: ${event}\n` : ''}${lines.join('\n')}\n\n`;
}

/**
 * A complete chat answer in the order the orchestrator sends it: the leading
 * handshake meta, a status line, the text in pieces, the engine's meta with
 * the ids folded in, done.
 *
 * @param {string} answer
 * @param {{pieces?: number, generationId?: string, intentId?: string|null, attempt?: number, sessionId?: string|null}} [opts]
 *   Without `intentId` (a client that sent none) the leading meta is omitted,
 *   exactly as the orchestrator omits it.
 */
function chatStubStream(answer, { pieces = 4, generationId = null, intentId = null, attempt = 1, sessionId = null } = {}) {
  const text = String(answer);
  const size = Math.max(1, Math.ceil(text.length / pieces));
  const ids = generationId
    ? {
        generation_id: generationId,
        trace_id: `e2e-trace-${generationId.slice(0, 8)}`,
        request_id: `e2e-request-${generationId.slice(0, 8)}`,
        ...(intentId ? { intent_id: intentId, attempt } : {}),
      }
    : {};
  let body = ': stub stream from e2e/platform\n\n';
  if (generationId && intentId) body += sseFrame('meta', ids);
  body += sseFrame('status', { text: 'Thinking' });
  for (let i = 0; i < text.length; i += size) {
    body += sseFrame('token', { text: text.slice(i, i + size) });
  }
  body += sseFrame('meta', { engine: 'e2e-stub', sources: [], ...ids });
  body += sseFrame('done', sessionId ? { session_id: sessionId } : {});
  return body;
}

/** A Responses-API stream as the console playground reads it. */
function playgroundStubStream(answer, model = 'techsara-35b') {
  const text = String(answer);
  let seq = 0;
  const next = () => (seq += 1);
  const response = {
    id: 'resp_e2estub',
    object: 'response',
    model,
    status: 'completed',
    usage: { input_tokens: 5, output_tokens: 3, total_tokens: 8 },
  };
  let body = '';
  body += sseFrame('response.created', { type: 'response.created', sequence_number: next(), response: { ...response, status: 'in_progress' } });
  for (const piece of text.match(/.{1,6}/g) || []) {
    body += sseFrame('response.output_text.delta', { type: 'response.output_text.delta', sequence_number: next(), delta: piece });
  }
  body += sseFrame('response.output_text.done', { type: 'response.output_text.done', sequence_number: next(), text });
  body += sseFrame('response.completed', { type: 'response.completed', sequence_number: next(), response });
  return body;
}

module.exports = { sseFrame, chatStubStream, playgroundStubStream };
