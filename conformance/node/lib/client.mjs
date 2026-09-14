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
 * A client with no CLIENT-side timer: the SDK timer at its ceiling, AND undici's
 * own headers/body timeouts (300 s each by default in Node's built-in fetch)
 * turned off through a matching undici `fetch` + `Agent`. Setting only
 * `timeout` leaves the 300 s headers timeout in place.
 *
 * WHAT IT IS FOR (2026-09-13, review): long STREAMS, and synchronous calls to a
 * self-hosted origin reached directly. It does not make a long synchronous call
 * work through the public hostname — Cloudflare answers 524 after 100 s with no
 * byte (CONTRACT-3 §8.3); use `stream: true` or `background: true` with an
 * Idempotency-Key there.
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
