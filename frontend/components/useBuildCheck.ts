'use client';

/**
 * Reload a tab that runs an old build (STORE-ALWAYS §3, 2026-10-03).
 * lib/buildCheck.ts holds the comparison and explains why.
 *
 * The host says when a reload would lose nothing (`quiet`: no draft, no
 * attachment, no send or stream in flight) and what to do just before one
 * (`prepare`: keep the typed text when the person asked for the reload, let
 * the history store finish its pushes). A new build found while quiet reloads
 * at once; otherwise `newBuild` turns on for the banner, and the page reloads
 * by itself the moment it becomes quiet — asked once a second, locally, with
 * no request.
 *
 * At most one reload by itself per server build: when the page that comes
 * back still carries the old id (a cache in between), the banner stays and
 * the reload is left to the person — never a loop.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  BUILD_CHECK_EVERY_MS,
  clearReloadedFor,
  createBuildCheck,
  markReloadedFor,
  pageBuildId,
  reloadedFor,
  reloadPage,
} from '@/lib/buildCheck';

/** How often "would a reload lose anything now?" is asked once a new build is known. */
const QUIET_POLL_MS = 1_000;

export function useBuildCheck(
  quiet: () => boolean,
  prepare: (keepDraft: boolean) => Promise<void>,
): { newBuild: boolean; reloadNow: () => void } {
  const [newBuild, setNewBuild] = useState<string | null>(null);
  const quietRef = useRef(quiet);
  quietRef.current = quiet;
  const prepareRef = useRef(prepare);
  prepareRef.current = prepare;
  const reloading = useRef(false);

  useEffect(() => {
    const pageBuild = pageBuildId();
    // A page with no build id (dev, a server before this change) never checks.
    if (!pageBuild) return;
    // This page is the build a reload was made for: the guard is spent.
    if (reloadedFor() === pageBuild) clearReloadedFor();
    const check = createBuildCheck({ pageBuild, onNewBuild: (server) => setNewBuild(server) });
    const run = () => {
      if (!document.hidden) void check.check();
    };
    document.addEventListener('visibilitychange', run);
    window.addEventListener('focus', run);
    const timer = window.setInterval(run, BUILD_CHECK_EVERY_MS);
    return () => {
      document.removeEventListener('visibilitychange', run);
      window.removeEventListener('focus', run);
      window.clearInterval(timer);
    };
  }, []);

  const reload = useCallback(async (server: string, keepDraft: boolean) => {
    if (reloading.current) return;
    reloading.current = true;
    try {
      await prepareRef.current(keepDraft);
    } catch {
      // Preparing is a courtesy; the old code must still go.
    }
    // QA 2026-10-03: preparing waits for the history store (up to 3 s), and
    // the person may start typing, attaching or sending in that time. A
    // reload nobody asked for is never worth that: let it go, and the quiet
    // poll below tries again once nothing would be lost.
    if (!keepDraft && !quietRef.current()) {
      reloading.current = false;
      return;
    }
    markReloadedFor(server);
    reloadPage();
  }, []);

  useEffect(() => {
    if (!newBuild || reloadedFor() === newBuild) return;
    const attempt = () => {
      if (!reloading.current && quietRef.current()) void reload(newBuild, false);
    };
    attempt();
    const timer = window.setInterval(attempt, QUIET_POLL_MS);
    return () => window.clearInterval(timer);
  }, [newBuild, reload]);

  const reloadNow = useCallback(() => {
    if (newBuild) void reload(newBuild, true);
  }, [newBuild, reload]);
  return { newBuild: newBuild !== null, reloadNow };
}
