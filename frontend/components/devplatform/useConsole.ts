'use client';

/**
 * Data loading for the console.
 *
 * Same contract as `useAnalytics` — `{data, loading, error, reload}` — and
 * every request aborts the one before it so clicking two projects quickly cannot leave the
 * slower query's answer on the page. It exists separately only because
 * `useAnalytics` is bound to `adminJson` and its `/api/admin/` prefix, and the
 * console rides its own BFF (see api.ts for why).
 *
 * STALE DATA SURVIVES A RELOAD, NOT A CHANGE OF QUESTION (2026-09-13). The
 * analytics hook keeps the last answer on screen through any refetch, because
 * there the query is a filter over one dataset. Here the path names WHICH
 * project: the review switched the picker from Alpha to Bravo and measured
 * Alpha's keys still listed, with no skeleton, under Bravo's name — on the
 * page whose row menu revokes a key. So data is kept through `reload()` (same
 * path, same query) and dropped the moment the path or the query changes.
 *
 * A 401 is swallowed: `consoleJson` has already started the sign-out, and an
 * error panel that flashes up behind a redirect is noise.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { AdminApiError, consoleJson } from './api';

export interface ConsoleQuery<T> {
  data: T | null;
  loading: boolean;
  /** Set only when the request for the CURRENT path and query failed. */
  error: string | null;
  reload: () => void;
}

export function useConsole<T>(
  path: string,
  params: Record<string, string | number | undefined> = {},
  /** Pass false to hold the request — a panel that needs a project first. */
  enabled = true,
): ConsoleQuery<T> {
  // The params object is rebuilt every render, so the effect keys off its
  // serialised form rather than its identity — otherwise every parent render
  // fires a fresh request.
  const query = new URLSearchParams(
    Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== '')
      .map(([k, v]) => [k, String(v)]),
  ).toString();
  const identity = `${path}?${query}`;

  // The answer is stored WITH the question it answers, and read back only
  // while that is still the question. Deriving it in render (rather than
  // clearing it in an effect) means there is not even one painted frame of
  // the previous project's rows under the new project's name.
  const [answer, setAnswer] = useState<{ identity: string; body: T } | null>(null);
  const [failure, setFailure] = useState<{ identity: string; message: string } | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [attempt, setAttempt] = useState(0);
  const inflight = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    inflight.current?.abort();
    const controller = new AbortController();
    inflight.current = controller;
    let live = true;
    setLoading(true);
    consoleJson<T>(`${path}${query ? `?${query}` : ''}`, {
      signal: controller.signal,
    })
      .then((body) => {
        if (!live || controller.signal.aborted) return;
        setAnswer({ identity, body });
        setFailure(null);
      })
      .catch((err: unknown) => {
        // An abort is this hook's own doing, not a failure to report.
        if (!live || controller.signal.aborted) return;
        if (err instanceof AdminApiError && err.status === 401) return;
        setFailure({
          identity,
          message:
            err instanceof AdminApiError ? err.message : 'This could not be loaded.',
        });
      })
      .finally(() => {
        if (live && !controller.signal.aborted) setLoading(false);
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [path, query, identity, attempt, enabled]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  const current = answer !== null && answer.identity === identity;
  return {
    data: current ? answer.body : null,
    // A question with no answer of its own yet is loading, even in the render
    // before the effect has had a chance to say so.
    loading: enabled && (loading || (!current && failure?.identity !== identity)),
    error: failure !== null && failure.identity === identity ? failure.message : null,
    reload,
  };
}
