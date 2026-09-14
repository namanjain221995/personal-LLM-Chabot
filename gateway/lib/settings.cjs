'use strict';
/**
 * Every number the v1-gateway runs on, read ONCE at start from the
 * environment (2026-09-13, no-timeout design revision 2, timers table).
 *
 * WHY A RESTART-SCOPED READ, when route.ts reads its caps per call: this
 * process is recreated only when the git tree sha of gateway/ changes, so a
 * setting that moved without a restart would be a setting nobody could say
 * was in force. The body caps are the exception and are read per request by
 * bodies.cjs from the SAME variables route.ts and the orchestrator read, so
 * the three halves refuse at the same byte.
 *
 * Parsing follows route.ts `envBytes`: blank, non-numeric, zero or negative
 * means "use the default". Seconds may be fractional; the tests scale the
 * 1,800 s re-attach budget down to seconds rather than wait half an hour.
 */

const MiB = 1024 * 1024;

function positive(env, name, fallback) {
  const raw = String(env[name] ?? '').trim();
  if (raw === '') return fallback;
  const value = Number(raw);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

/** Like `positive`, but 0 is a value: disk_ledger.py reads its floor with max(0, int(...)). */
function nonNegativeInt(env, name, fallback) {
  const raw = String(env[name] ?? '').trim();
  if (raw === '') return fallback;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? Math.floor(value) : fallback;
}

function seconds(env, name, fallbackSeconds) {
  return Math.round(positive(env, name, fallbackSeconds) * 1000);
}

function port(env, name, fallback) {
  const raw = String(env[name] ?? '').trim();
  if (raw === '') return fallback;
  const value = Number(raw);
  // 0 is legal here (tests ask the kernel for a free port).
  return Number.isInteger(value) && value >= 0 && value < 65536 ? value : fallback;
}

function orchestratorBase(env) {
  // lib/proxy.ts `orchestratorUrl()`: the same variable, the same default.
  const raw = String(env.ORCHESTRATOR_URL ?? 'http://localhost:8080').trim();
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw new Error('ORCHESTRATOR_URL is not a URL');
  }
  if (url.protocol !== 'http:') {
    // node:http only, on purpose: fetch/undici would bring back the 300 s
    // headers/body timers this process exists to avoid (timers table,
    // "v1-gateway upstream"). The orchestrator is on the internal network.
    throw new Error('ORCHESTRATOR_URL must be an http:// URL');
  }
  return { hostname: url.hostname, port: Number(url.port || 80) };
}

function attachMode(env) {
  const raw = String(env.V1_GATEWAY_ATTACH ?? '').trim().toLowerCase();
  return raw === 'on' || raw === 'off' ? raw : 'auto';
}

function readSettings(env = process.env) {
  return Object.freeze({
    host: String(env.V1_GATEWAY_HOST ?? '').trim() || '0.0.0.0',
    port: port(env, 'V1_GATEWAY_PORT', 8090),
    orchestrator: orchestratorBase(env),

    // --- client side (timers table, "v1-gateway client side") ------------
    // requestTimeout 0: no response-duration timer at all. headersTimeout
    // 100 s is the time a client has to finish SENDING its headers.
    // keepAliveTimeout 95 s sits above cloudflared's 90 s keepAliveTimeout,
    // so the idle connection is always closed by cloudflared first and a
    // request never lands on a socket this side is closing.
    requestTimeoutMs: 0,
    headersTimeoutMs: 100_000,
    keepAliveTimeoutMs: 95_000,
    bodyIdleMs: seconds(env, 'V1_GATEWAY_BODY_IDLE_S', 60),

    // --- the byte invariant (CONTRACT §10) --------------------------------
    // No client waits more than this for a byte once the orchestrator has
    // the request, and no gap after that exceeds it.
    heartbeatMs: seconds(env, 'V1_GATEWAY_HEARTBEAT_S', 15),

    // --- upstream (timers table, "v1-gateway upstream") -------------------
    upstreamSilenceMs: seconds(env, 'V1_GATEWAY_UPSTREAM_SILENCE_S', 300),
    precommitRetryMs: seconds(env, 'PUBLIC_API_GATEWAY_PRECOMMIT_RETRY_S', 110),
    precommitRetryIntervalMs: seconds(env, 'V1_GATEWAY_PRECOMMIT_RETRY_INTERVAL_S', 2),
    reattachMaxMs: seconds(env, 'PUBLIC_API_GATEWAY_REATTACH_MAX_S', 1800),
    reattachBackoffMinMs: seconds(env, 'V1_GATEWAY_REATTACH_BACKOFF_MIN_S', 1),
    reattachBackoffMaxMs: seconds(env, 'V1_GATEWAY_REATTACH_BACKOFF_MAX_S', 10),
    // How long a relayed SSE data frame may wait for its `: ts-seq=N`
    // comment before it is released unconfirmed (sse.cjs, relay.cjs).
    seqHoldMs: seconds(env, 'V1_GATEWAY_SEQ_HOLD_S', 1),
    attach: attachMode(env),

    // --- container stop ("v1-gateway container stop") ---------------------
    drainAbortMs: seconds(env, 'V1_GATEWAY_DRAIN_ABORT_S', 2),

    // --- physical safety ----------------------------------------------------
    fdPressureRatio: Math.min(1, positive(env, 'V1_GATEWAY_FD_PRESSURE_RATIO', 0.7)),
    memoryBodyBytes: Math.floor(positive(env, 'V1_GATEWAY_MEMORY_BODY_BYTES', MiB)),
    // lib/guards.cjs MemoryBudget: body bytes held in memory across every
    // relay; past it bodies spill to the spool (review 2026-09-14).
    memoryBudgetBytes: Math.floor(positive(env, 'V1_GATEWAY_MEMORY_BUDGET_BYTES', 256 * MiB)),
    spoolDir: String(env.V1_GATEWAY_SPOOL_DIR ?? '').trim() || '/spool',
    // lib/guards.cjs SpoolBudget: a total across relays, and a free-space
    // floor shared with the orchestrator's publicapi/disk_ledger.py.
    spoolMaxBytes: Math.floor(positive(env, 'V1_GATEWAY_SPOOL_MAX_BYTES', 2 * 1024 * MiB)),
    spoolMinFreeBytes: nonNegativeInt(env, 'PUBLIC_API_MIN_FREE_DISK_BYTES', 20 * 1024 * MiB),
    earlyResponseMaxBytes: MiB,

    // --- lib/proxy.ts's trust settings, same names --------------------------
    trustedClientIpHeader: String(env.TRUSTED_CLIENT_IP_HEADER ?? '').trim().toLowerCase(),
    trustedForwardedProto: (() => {
      const value = String(env.TRUSTED_FORWARDED_PROTO ?? '').trim().toLowerCase();
      return value === 'https' || value === 'http' ? value : null;
    })(),

    env,
  });
}

module.exports = { readSettings, MiB };
