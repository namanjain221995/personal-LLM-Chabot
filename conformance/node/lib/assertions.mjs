// Shared assertions for the live suite.
import assert from 'node:assert/strict';

/**
 * CONTRACT-3 §9: "No response body may contain a traceback, SQL, an environment
 * value, a container name, an internal hostname, a filesystem path, a private IP,
 * an internal checkpoint name or an engine URL." These are the spellings this
 * deployment's internals actually use; a public id like `techsara-whisper` is fine.
 */
const INTERNAL = [
  /qwen/i,
  /nvfp4/i,
  /unlimited-ocr/i,
  /baidu/i,
  /whisper-large/i,
  /vllm/i,
  /sf-local-ai/i,
  /Traceback \(most recent call last\)/,
  /\/models\/repos\//,
  /\b(?:10\.\d{1,3}|127\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b/,
  /https?:\/\/(?!ai\.techsarasolutions\.com)[a-z0-9.-]+:\d+/i,
];

export function assertNoInternalNames(value, where = 'body') {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  for (const re of INTERNAL) {
    assert.ok(!re.test(text), `${where} matches ${re}: ${text.slice(0, 300)}`);
  }
}

/** CONTRACT-3 §9 envelope on an SDK APIError. */
export function assertEnvelope(err, { status, code, type, param } = {}) {
  assert.ok(err && typeof err === 'object', `expected an error, got ${err}`);
  if (status !== undefined) assert.equal(err.status, status, `status; message: ${err.message}`);
  assert.ok(err.error && typeof err.error === 'object', `no envelope on ${err.constructor?.name}: ${err.message}`);
  if (code !== undefined) assert.equal(err.code, code, `code; message: ${err.message}`);
  if (type !== undefined) assert.equal(err.type, type, `type; message: ${err.message}`);
  if (param !== undefined) assert.equal(err.param, param, `param; message: ${err.message}`);
  assert.equal(typeof err.error.message, 'string');
  assert.match(String(err.error.request_id), /^req_/, 'envelope request_id');
  assert.equal(err.requestID, err.error.request_id, 'X-Request-Id header equals envelope request_id');
  assertNoInternalNames(err.error, 'error envelope');
}

export async function rejection(promise) {
  try {
    await promise;
  } catch (err) {
    return err;
  }
  assert.fail('expected the call to fail');
}

/** usage is an object of three consistent counts, or null (never 0 for "not measured"). */
export function assertUsage(usage, { inputKey = 'input_tokens', outputKey = 'output_tokens' } = {}) {
  if (usage === null) return;
  assert.ok(usage && typeof usage === 'object', `usage: ${JSON.stringify(usage)}`);
  assert.ok(Number.isInteger(usage[inputKey]) && usage[inputKey] > 0, `usage.${inputKey}: ${JSON.stringify(usage)}`);
  assert.ok(Number.isInteger(usage[outputKey]) && usage[outputKey] > 0, `usage.${outputKey}: ${JSON.stringify(usage)}`);
  assert.equal(usage.total_tokens, usage[inputKey] + usage[outputKey], 'total_tokens = input + output');
}
