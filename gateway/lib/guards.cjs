'use strict';
/**
 * Physical guards (2026-09-13, no-timeout design F "Physical safety").
 *
 * With no timer ending a /v1 request, the only things bounding a flood of
 * patient connections are physical: file descriptors and disk. Each relay
 * holds two sockets (client, orchestrator) and, for a spilled body, one file.
 *
 * MEASURED: Node raises its soft RLIMIT_NOFILE to the hard limit at start
 * (soft 1024 became 500000 on this host), so the ceiling is the hard limit,
 * read back from /proc/self/limits rather than assumed. Above 70% of it new
 * /v1 requests get 503 Retry-After 30 before any header is written — a
 * refusal an SDK retries — while /healthz keeps answering, so the
 * orchestrator of this relay is not mistaken for a dead one.
 *
 * The count is sampled at most once a second: readdir of /proc/self/fd is
 * O(open fds), and at 100k fds per request it would be the flood's ally.
 */

const fs = require('node:fs');

function parseNofile(limitsText) {
  const line = String(limitsText)
    .split('\n')
    .find((l) => l.startsWith('Max open files'));
  if (!line) return null;
  const fields = line.slice('Max open files'.length).trim().split(/\s+/);
  const toNumber = (v) => (v === 'unlimited' ? Infinity : Number(v));
  const soft = toNumber(fields[0]);
  const hard = toNumber(fields[1]);
  if (!Number.isFinite(soft) && soft !== Infinity) return null;
  return { soft, hard };
}

function readNofile() {
  try {
    return parseNofile(fs.readFileSync('/proc/self/limits', 'utf8'));
  } catch {
    return null;
  }
}

function openFdCount() {
  try {
    return fs.readdirSync('/proc/self/fd').length;
  } catch {
    return null;
  }
}

class FdGuard {
  constructor({ ratio = 0.7, sampleMs = 1000, now = () => Date.now(), limits = readNofile, count = openFdCount } = {}) {
    this.ratio = ratio;
    this.sampleMs = sampleMs;
    this.now = now;
    this.limits = limits;
    this.count = count;
    this.sampledAt = -Infinity;
    this.cached = 0;
  }

  /** open fds / soft limit, in [0, 1+]; 0 when it cannot be measured. */
  pressure() {
    const t = this.now();
    if (t - this.sampledAt < this.sampleMs) return this.cached;
    this.sampledAt = t;
    const lim = this.limits();
    const open = this.count();
    this.cached = lim && open !== null && Number.isFinite(lim.soft) && lim.soft > 0 ? open / lim.soft : 0;
    return this.cached;
  }

  overPressure() {
    return this.pressure() >= this.ratio;
  }
}

/**
 * The spool's disk budget (2026-09-13, review finding, reproduced with
 * scratchpad p3_spool.cjs: six connections with no credential, each sending
 * 19 MiB of a declared 20 MiB and then one byte every 1.5 s, held six spool
 * files, 114 MiB, for as long as they liked, with the orchestrator never
 * called; the only bound was the any-byte idle guard). The spool volume sits
 * on the host's root filesystem next to sf-local-ai_pgdata, so an unbounded
 * spool is a Postgres outage, and the design's physical-safety section
 * requires disk guards.
 *
 * Two limits, both checked BEFORE a byte is written:
 *  - a total across every relay (V1_GATEWAY_SPOOL_MAX_BYTES, default 2 GiB:
 *    100 bodies at the 20 MiB media cap, or ~30 upload parts spooled during
 *    one orchestrator restart window);
 *  - a free-space floor read with statfs (PUBLIC_API_MIN_FREE_DISK_BYTES,
 *    default 20 GiB, the same variable and default as the orchestrator's
 *    publicapi/disk_ledger.py floor), so the edge refuses before pushing the
 *    device under the line the orchestrator itself defends.
 * statfs is sampled at most once a second; reservations made since the
 * sample are subtracted, so a burst inside one second cannot overshoot.
 */
class SpoolBudget {
  constructor({ dir, maxBytes, minFreeBytes, sampleMs = 1000, now = () => Date.now(), statfs = (d) => fs.statfsSync(d) } = {}) {
    this.dir = dir;
    this.maxBytes = maxBytes;
    this.minFreeBytes = minFreeBytes;
    this.sampleMs = sampleMs;
    this.now = now;
    this.statfs = statfs;
    this.reserved = 0;
    this.sampledAt = -Infinity;
    this.sampledFree = null;
    this.reservedSinceSample = 0;
  }

  /** Free bytes on the spool's filesystem, or null when it cannot be read. */
  free() {
    const t = this.now();
    if (t - this.sampledAt >= this.sampleMs) {
      this.sampledAt = t;
      this.reservedSinceSample = 0;
      try {
        const st = this.statfs(this.dir);
        this.sampledFree = Number(st.bavail) * Number(st.bsize);
      } catch {
        this.sampledFree = null;
      }
    }
    return this.sampledFree === null ? null : this.sampledFree - this.reservedSinceSample;
  }

  /** { ok: true } and the bytes are held, or { ok: false, reason: 'quota' | 'disk' }. */
  reserve(bytes) {
    const n = Math.max(0, Math.ceil(bytes));
    if (this.reserved + n > this.maxBytes) return { ok: false, reason: 'quota' };
    const free = this.free();
    // An unreadable filesystem is not refused here: the quota still holds,
    // and a spool that cannot be written fails as a storage error.
    if (free !== null && free - n < this.minFreeBytes) return { ok: false, reason: 'disk' };
    this.reserved += n;
    this.reservedSinceSample += n;
    return { ok: true };
  }

  release(bytes) {
    this.reserved = Math.max(0, this.reserved - Math.max(0, Math.ceil(bytes)));
  }
}

module.exports = { parseNofile, readNofile, openFdCount, FdGuard, SpoolBudget };
