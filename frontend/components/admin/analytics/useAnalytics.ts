'use client';

/**
 * Data loading for the analytics console.
 *
 * One hook, one contract: `{data, loading, error, reload}`. It matters that
 * `data` SURVIVES a reload — when the range changes, the previous numbers
 * stay on screen dimmed rather than being replaced by skeletons, so the page
 * does not flash empty every time a filter moves.
 *
 * Every request is abortable and the last one wins. Clicking through 7d →
 * 30d → 90d quickly used to be a race whose winner was whichever query the
 * database happened to finish first; here each new request aborts the one
 * before it, and a response that arrives after its own abort is discarded.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { AdminApiError, adminJson } from '../api';

export interface Query<T> {
  data: T | null;
  loading: boolean;
  /** Set only when the CURRENT request failed; stale data may still show. */
  error: string | null;
  reload: () => void;
}

export function useAnalytics<T>(
  path: string,
  params: Record<string, string | number | undefined> = {},
): Query<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const inflight = useRef<AbortController | null>(null);

  // The params object is rebuilt on every render, so the effect keys off its
  // serialised form — otherwise every parent render refetches.
  const query = new URLSearchParams(
    Object.entries(params)
      .filter(([, v]) => v !== undefined && v !== '')
      .map(([k, v]) => [k, String(v)]),
  ).toString();

  useEffect(() => {
    inflight.current?.abort();
    const controller = new AbortController();
    inflight.current = controller;
    let live = true;
    setLoading(true);
    adminJson<T>(`${path}${query ? `?${query}` : ''}`, {
      signal: controller.signal,
    })
      .then((body) => {
        if (!live || controller.signal.aborted) return;
        setData(body);
        setError(null);
      })
      .catch((err: unknown) => {
        // An abort is this hook's own doing, not a failure to report.
        if (!live || controller.signal.aborted) return;
        if (err instanceof AdminApiError && err.status === 401) return;
        setError(
          err instanceof AdminApiError
            ? err.message
            : 'This data could not be loaded.',
        );
      })
      .finally(() => {
        if (live && !controller.signal.aborted) setLoading(false);
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [path, query, attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);
  return { data, loading, error, reload };
}

/**
 * A value that lags behind its input by `delay`.
 *
 * The leaderboard search box types straight into the URL (so the view is
 * shareable) but must not fire a query per keystroke; this is what sits
 * between them.
 */
export function useDebouncedValue<T>(value: T, delay = 250): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return settled;
}
