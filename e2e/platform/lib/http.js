'use strict';
/**
 * A tiny HTTP client for the checks that do not need a browser.
 *
 * WHY NOT A COOKIE JAR LIBRARY (2026-09-13): the session cookie `ts_session`
 * is issued with `Secure`, and a spec-following jar refuses to send a Secure
 * cookie back over plain http — which is exactly how a loopback e2e stack or a
 * candidate container is reached. Chrome makes an exception for 127.0.0.1 and
 * localhost; a jar does not. So the cookie is parsed out of Set-Cookie and
 * pinned as a header, the same way scripts/devapi_smoke.py does it.
 */

const SESSION_COOKIE = 'ts_session';

/** Every `name=value` pair a response set, ignoring attributes. */
function parseSetCookies(headers) {
  const raw = typeof headers.getSetCookie === 'function' ? headers.getSetCookie() : [];
  const out = {};
  for (const line of raw) {
    const first = String(line).split(';', 1)[0];
    const eq = first.indexOf('=');
    if (eq <= 0) continue;
    out[first.slice(0, eq).trim()] = first.slice(eq + 1).trim();
  }
  return out;
}

class HttpClient {
  /**
   * @param {string} base  e.g. http://127.0.0.1:3001 — no trailing slash needed
   * @param {{timeoutMs?: number}} [opts]
   */
  constructor(base, opts = {}) {
    this.base = String(base).replace(/\/+$/, '');
    this.cookies = {};
    this.timeoutMs = opts.timeoutMs ?? 60_000;
    /** Throws when the owning check is over; see makeSessions. */
    this.guard = opts.guard || null;
  }

  cookieHeader() {
    return Object.entries(this.cookies)
      .map(([k, v]) => `${k}=${v}`)
      .join('; ');
  }

  /**
   * One request. Never follows redirects: a redirect to /login is an ANSWER
   * several checks assert on, and a followed one would read as a 200.
   * The Origin header is the base itself, which is what a same-origin browser
   * sends and what the proxies' CSRF checks expect.
   */
  async request(method, path, { json, body, headers = {}, auth = true, timeoutMs, afterDispose = false } = {}) {
    // A check that timed out keeps running (a promise cannot be cancelled);
    // its clients refuse new requests once the runner has moved on, so its
    // polling loops end at their next call instead of writing to the stack.
    if (this.guard && !afterDispose) this.guard();
    const url = /^https?:\/\//.test(path) ? path : `${this.base}${path}`;
    const h = { origin: this.base, ...headers };
    if (auth && Object.keys(this.cookies).length) h.cookie = this.cookieHeader();
    let payload = body;
    if (json !== undefined) {
      h['content-type'] = 'application/json';
      payload = JSON.stringify(json);
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs ?? this.timeoutMs);
    let res;
    try {
      res = await fetch(url, {
        method,
        headers: h,
        body: payload,
        redirect: 'manual',
        signal: controller.signal,
        ...(payload && typeof payload.getReader === 'function' ? { duplex: 'half' } : {}),
      });
    } finally {
      clearTimeout(timer);
    }
    const set = parseSetCookies(res.headers);
    for (const [k, v] of Object.entries(set)) {
      if (v === '' || v === '""') delete this.cookies[k];
      else this.cookies[k] = v;
    }
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }
    return {
      status: res.status,
      headers: res.headers,
      location: res.headers.get('location'),
      text,
      json: data,
      setCookies: set,
    };
  }

  get(path, opts) { return this.request('GET', path, opts); }
  post(path, opts) { return this.request('POST', path, opts); }
  put(path, opts) { return this.request('PUT', path, opts); }
  patch(path, opts) { return this.request('PATCH', path, opts); }
  delete(path, opts) { return this.request('DELETE', path, opts); }

  /** Sign in through the frontend's own BFF, exactly as the login form does. */
  async login(email, password) {
    const res = await this.post('/api/auth/login', { json: { email, password } });
    if (res.status !== 200 || !this.cookies[SESSION_COOKIE]) {
      const err = new Error(
        `login as ${email} failed: HTTP ${res.status}; ${SESSION_COOKIE} cookie ${
          this.cookies[SESSION_COOKIE] ? 'set' : 'absent'
        }; body ${truncate(res.text, 300)}`,
      );
      err.response = res;
      throw err;
    }
    return res;
  }
}

function truncate(text, n) {
  const s = String(text ?? '');
  return s.length > n ? `${s.slice(0, n)}… (${s.length} chars)` : s;
}

module.exports = { HttpClient, SESSION_COOKIE, parseSetCookies, truncate };
