'use strict';
/**
 * Stopping the gateway (2026-09-13, timers table "v1-gateway container stop").
 *
 * On SIGTERM: stop listening, close idle keep-alive sockets at once, and
 * DESTROY every relay still running 2 s later, then exit.
 *
 * WHY DESTROY, when the frontend's old handler waits for every response:
 * "server.close() waits for every in-flight response, so one long relay holds
 * the whole site for 5 min" (the frontend finding this design fixes). A /v1
 * relay can legitimately run for hours, so waiting is not a drain, it is an
 * outage. An incomplete read is the one signal every client acts on: SDK
 * retries implicitly attach (x-stainless-retry-count ≥1, same key, same body
 * sha) and the documented resume loop rejoins the run, while the
 * orchestrator keeps generating through its orphan grace (120 s unkeyed,
 * 600 s keyed). The 2 s lets a response that is about to finish, finish.
 *
 * The gateway is recreated only when gateway/ changes, so this runs rarely;
 * compose's stop_grace_period 30s is far above the 2 s it needs.
 */

function installDrain({
  server,
  registry,
  settings,
  log,
  onStart = () => undefined,
  exit = (code) => process.exit(code),
  signals = ['SIGTERM', 'SIGINT'],
}) {
  let draining = false;
  let exited = false;

  const finish = (reason) => {
    if (exited) return;
    exited = true;
    log('drain_exit', { reason, relays_left: registry.size });
    // Let the log line and any destroy() reach the kernel before exiting.
    setImmediate(() => exit(0));
  };

  const drain = (signal) => {
    if (draining) return;
    draining = true;
    onStart(signal);
    log('drain_start', { signal, relays: registry.size });
    server.close();
    if (typeof server.closeIdleConnections === 'function') server.closeIdleConnections();

    const poll = setInterval(() => {
      if (registry.size === 0) {
        clearInterval(poll);
        clearTimeout(abortTimer);
        if (typeof server.closeAllConnections === 'function') server.closeAllConnections();
        finish('idle');
      }
    }, 50);

    const abortTimer = setTimeout(() => {
      clearInterval(poll);
      const relays = [...registry];
      for (const relay of relays) relay.abortForDrain();
      if (typeof server.closeAllConnections === 'function') server.closeAllConnections();
      log('drain_aborted', { aborted: relays.length });
      finish('aborted');
    }, settings.drainAbortMs);
  };

  const handlers = signals.map((signal) => {
    const handler = () => drain(signal);
    process.on(signal, handler);
    return [signal, handler];
  });

  return {
    drain,
    get draining() {
      return draining;
    },
    uninstall() {
      for (const [signal, handler] of handlers) process.off(signal, handler);
    },
  };
}

module.exports = { installDrain };
