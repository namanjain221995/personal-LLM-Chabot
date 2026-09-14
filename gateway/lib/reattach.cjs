'use strict';
/**
 * Post-commit survival: when may the gateway re-attach, how, and for how
 * long (2026-09-13, no-timeout design, INTERNAL ATTACH PROTOCOL and the
 * timers table "v1-gateway upstream").
 *
 * WHY. With ~5 deploys a day a 3-hour call crosses an orchestrator restart
 * with probability 1 - e^(-0.625) ≈ 46%. Once a byte has reached the client
 * the status line is spent, so the only honest options are "keep the client
 * alive and fetch the rest" or "cut the connection so the SDK knows". This
 * module decides which, and the loop keeps trying for up to 1,800 s of
 * CONTINUOUS orchestrator absence: 1 → 2 → 4 → 8 → 10 s backoff, plus one
 * final attempt exactly at the deadline, so an orchestrator back at 1,799 s
 * is found and one back at 1,801 s is not.
 *
 * Everything here is pure or clock-injected; relay.cjs owns the sockets.
 */

const { isEventStream } = require('./headers.cjs');

function backoffMs(index, minMs = 1000, maxMs = 10_000) {
  const value = minMs * 2 ** Math.max(0, index);
  return Math.min(maxMs, value);
}

/**
 * The run named by X-TechSara-Run: a response id, `job:<key>` (an audio job,
 * re-attached with X-TechSara-Attach-Job and an empty body), `none` (a run
 * that cannot be replayed: store:false, or a route with no run), or absent
 * (an orchestrator that does not speak the protocol).
 */
function parseRun(value) {
  if (typeof value !== 'string' || value.trim() === '') return { kind: 'absent' };
  const v = value.trim();
  if (v === 'none') return { kind: 'none' };
  if (v.startsWith('job:') && v.length > 4) return { kind: 'job', key: v.slice(4) };
  return { kind: 'response', id: v };
}

/**
 * May a committed relay whose upstream failed be re-attached?
 *
 * state: {
 *   shape: 'sse' | 'json' | 'opaque',
 *   run: parseRun(...) of the last upstream answer, or { kind: 'absent' },
 *   terminal: an SSE terminal event was relayed,
 *   jsonStarted: a non-whitespace JSON byte was relayed,
 *   seqReliable: every relayed SSE event was confirmed by a ts-seq,
 *   lastSeq: the last confirmed sequence number (0 when none),
 *   replayable: the request body can be sent again byte for byte,
 *   deterministic: the route recomputes identically (embeddings, rerank),
 *   attach: 'on' | 'off' | 'auto', evidence: the orchestrator has answered
 *     with X-TechSara-Run since this process started,
 * }
 *
 * Returns { ok: true, headers, emptyBody } or { ok: false, reason }.
 */
function planReattach(state) {
  if (state.shape === 'sse' && state.terminal) return { ok: false, reason: 'terminal' };
  if (state.attach === 'off') return { ok: false, reason: 'attach_off' };
  if (state.shape === 'opaque') return { ok: false, reason: 'opaque_body' };
  if (state.shape === 'json' && state.jsonStarted) return { ok: false, reason: 'partial_json' };

  const run = state.run || { kind: 'absent' };
  if (run.kind === 'none') {
    if (state.shape === 'json' && state.deterministic && state.replayable) {
      return { ok: true, headers: {}, emptyBody: false };
    }
    return { ok: false, reason: 'not_replayable' };
  }
  if (run.kind === 'job') {
    // A job stream re-attaches with Resume-After too (relay.cjs), so a frame
    // released without its ts-seq would be replayed to the client twice.
    if (state.shape === 'sse' && !state.seqReliable) return { ok: false, reason: 'sequence_unconfirmed' };
    return { ok: true, headers: { 'x-techsara-attach-job': run.key }, emptyBody: true };
  }
  if (run.kind === 'absent') {
    // No answer carried the header on THIS relay (a silent orchestrator the
    // gateway committed for). Only re-POST when the orchestrator has shown it
    // attaches; otherwise a re-POST to an orchestrator that ignores the
    // attempt id launches the same generation twice.
    const speaks = state.attach === 'on' || state.evidence === true;
    if (!speaks) {
      return state.shape === 'json' && state.deterministic && state.replayable
        ? { ok: true, headers: {}, emptyBody: false }
        : { ok: false, reason: 'protocol_unknown' };
    }
  }
  if (!state.replayable) return { ok: false, reason: 'body_not_replayable' };
  if (state.shape === 'sse') {
    if (!state.seqReliable) return { ok: false, reason: 'sequence_unconfirmed' };
    return {
      ok: true,
      headers: { 'x-techsara-resume-after': String(state.lastSeq || 0) },
      emptyBody: false,
    };
  }
  return { ok: true, headers: {}, emptyBody: false };
}

/**
 * What to do with the orchestrator's answer to a re-attach:
 * 'relay' (continue the client's body), 'retry' (keep waiting), 'abort'.
 * 404 is the orchestrator saying the run exists no more (events expired,
 * store:false); every other refusal is equally final, except the three
 * statuses that mean "not now".
 */
function classifyAttachResponse(status, contentType, shape) {
  if (status === 200) {
    const sse = isEventStream(contentType);
    if (shape === 'sse' ? sse : !sse) return 'relay';
    return 'abort';
  }
  if (status === 502 || status === 503 || status === 504) return 'retry';
  return 'abort';
}

/**
 * Is a 200 answer to a re-attach the SAME run the client has been reading?
 *
 * WHY (2026-09-13, review finding, reproduced with scratchpad
 * p1_rollback.cjs): the status and the content type were the only checks.
 * An orchestrator rolled back to a build that ignores X-TechSara-* during the
 * restart launched the generation afresh, and its answer was spliced after
 * the frames the client already held: deltas run-A:A1..A3 then run-B:B1..B3,
 * a 200 the SDK read as one complete answer, generated and billed twice. A
 * rollback is a restart, which is exactly when re-attach runs.
 *
 *  - a response run must come back naming the same response id, a job the
 *    same job key;
 *  - a relay committed while the orchestrator was silent (no run seen) must
 *    come back naming SOME run: a fresh launch by a build that does not speak
 *    the protocol names none;
 *  - a JSON answer to an idempotent request (a GET, or the deterministic
 *    embeddings and rerank recomputation) is the same answer whoever computes
 *    it, and a whitespace-only JSON body has nothing to splice into.
 */
function attachAnswerMatches({ expected, header, shape, idempotent }) {
  if (shape === 'json' && idempotent) return true;
  const got = parseRun(header);
  const want = expected || { kind: 'absent' };
  if (want.kind === 'response') return got.kind === 'response' && got.id === want.id;
  if (want.kind === 'job') return got.kind === 'job' && got.key === want.key;
  if (want.kind === 'absent') return got.kind === 'response' || got.kind === 'job';
  return false;
}

/**
 * The loop. `tryOnce()` resolves { outcome: 'relay' | 'retry' | 'abort', ... }.
 * `sleep(ms)` resolves false when the relay was cancelled meanwhile.
 * Resolves the relay/abort result, or { outcome: 'exhausted', tries }.
 */
async function reattachLoop({ deadline, now, sleep, tryOnce, backoff }) {
  let tries = 0;
  for (let index = 0; ; index += 1) {
    const remaining = deadline - now();
    // A budget already spent by earlier cycles (an orchestrator that keeps
    // answering a head and dying) is not renewed by starting a new loop.
    if (remaining < 0) return { outcome: 'exhausted', tries };
    const wait = Math.max(0, Math.min(backoff(index), remaining));
    if (wait > 0) {
      const alive = await sleep(wait);
      if (!alive) return { outcome: 'cancelled', tries };
    }
    tries += 1;
    const result = await tryOnce();
    if (result.outcome === 'relay' || result.outcome === 'abort' || result.outcome === 'cancelled') {
      return { ...result, tries };
    }
    if (now() >= deadline) return { outcome: 'exhausted', tries };
  }
}

module.exports = { backoffMs, parseRun, planReattach, classifyAttachResponse, attachAnswerMatches, reattachLoop };
