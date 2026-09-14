// SDK clients and a raw-HTTP helper, both paced and both observable.
import OpenAI from 'openai';
import { Agent, fetch as undiciFetch } from 'undici';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import { env } from './env.mjs';

const PACE_FILE = join(dirname(fileURLToPath(import.meta.url)), '..', 'results', '.last-request-ms');

// WHY a file (2026-09-13): node:test runs each test file in its own process, so
// an in-memory "last request" clock would reset between files and a serial run
// could still burst past a 60 requests/minute project limit at file boundaries.
async function pace() {
  if (env.minIntervalMs <= 0) return;
  let last = 0;
  try {
    last = Number(readFileSync(PACE_FILE, 'utf8')) || 0;
  } catch {
    /* first request of the run */
  }
  const wait = last + env.minIntervalMs - Date.now();
  if (wait > 0) await sleep(wait);
  mkdirSync(dirname(PACE_FILE), { recursive: true });
  writeFileSync(PACE_FILE, String(Date.now()));
}

/**
 * The largest delay Node's setTimeout honours. Anything larger — including
 * Infinity — is coerced to 1 ms with a TimeoutOverflowWarning, and the SDK's
 * own timer (`setTimeout(abort, ms)`) then aborts the request at once.
 * Measured in test/offline/timeout.test.mjs.
 */
export const MAX_TIMER_MS = 2 ** 31 - 1;

/**
 * The client the documentation teaches (/docs/timeouts, 2026-09-13, no-timeout
 * design revision 2): the SDK timer at its ceiling and five retries, nothing
 * else. Against a no-timeout stack that is enough: the API writes a byte at least
 * every 15 s (CONTRACT-3 §10.1), which resets undici's 300 s header and body
 * timers in the caller's own process, and a retry of a running request attaches
 * to it (§13).
 */
export function documentedClientOptions() {
  return { timeout: MAX_TIMER_MS, maxRetries: 5 };
}

/**
 * A client with no CLIENT-side timer at all: the SDK timer at its ceiling, AND
 * undici's own headers/body timeouts (300 s each by default in Node's built-in
 * fetch) turned off through a matching undici `fetch` + `Agent`. Setting only
 * `timeout` leaves the 300 s headers timeout in place.
 *
 * WHAT IT IS FOR: a caller behind a proxy of its own that goes silent, and a
 * stack from before the no-timeout release, where a synchronous response sends
 * no byte until it is done. The documentation recommends it only for the first.
 */
export function noTimeoutOptions() {
  return {
    timeout: MAX_TIMER_MS,
    fetch: undiciFetch,
    fetchOptions: { dispatcher: new Agent({ headersTimeout: 0, bodyTimeout: 0 }) },
  };
}

/**
 * @param {object} [o]
 * @param {string} [o.apiKey]
 * @param {number} [o.maxRetries]
 * @param {boolean} [o.noTimeout]
 * @param {object} [o.clientOptions] extra OpenAI constructor options
 * @returns {{ client: OpenAI, requests: Array<{method:string,url:string,headers:Record<string,string>,status?:number}> }}
 */
export function makeClient(o = {}) {
  const requests = [];
  const base = o.noTimeout ? noTimeoutOptions() : {};
  const innerFetch = base.fetch || o.clientOptions?.fetch || globalThis.fetch;
  const recordingFetch = async (url, init = {}) => {
    await pace();
    const headers = {};
    new Headers(init.headers || {}).forEach((v, k) => {
      headers[k] = k === 'authorization' ? '<redacted>' : v;
    });
    const entry = { method: init.method || 'GET', url: String(url), headers };
    requests.push(entry);
    const res = await innerFetch(url, init);
    entry.status = res.status;
    return res;
  };
  const client = new OpenAI({
    baseURL: env.baseURL,
    apiKey: o.apiKey ?? env.apiKey,
    maxRetries: o.maxRetries ?? 2,
    ...base,
    ...(o.clientOptions || {}),
    fetch: recordingFetch,
  });
  return { client, requests };
}

/** Raw HTTP for routes the SDK has no method for, and for header-level checks. */
export async function raw(method, path, { body, headers = {}, apiKey = env.apiKey, form } = {}) {
  await pace();
  const h = { ...(apiKey ? { authorization: `Bearer ${apiKey}` } : {}), ...headers };
  let payload;
  if (form) payload = form;
  else if (body !== undefined) {
    h['content-type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const res = await fetch(`${env.baseURL}${path}`, { method, headers: h, body: payload });
  const text = await res.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    json = undefined;
  }
  return { status: res.status, headers: res.headers, text, json };
}

/** Pull the §9 envelope out of an SDK APIError. */
export function envelope(err) {
  return err && err.error ? err.error : undefined;
}
