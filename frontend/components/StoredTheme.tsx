'use client';

/**
 * The saved theme, applied from React for documents the inline script never
 * ran in.
 *
 * app/layout.tsx stamps `dark`/`light` on <html> with an inline script before
 * first paint. When a layout or page throws notFound() (the developer console
 * refusing a member, an unknown /docs slug), Next answers with its error shell
 * (`<html id="__next_error__">`) and renders the tree in the browser — and a
 * <script> that React inserts is never executed. Those 404 pages drew dark
 * whatever the reader had chosen (measured in Chrome, 2026-09-13: html class
 * "" with localStorage techsara.theme = "light").
 *
 * It does nothing when the script already ran, so it cannot fight a normal
 * page or the theme toggle. A layout effect, so the stored theme lands before
 * the browser paints the client-rendered page; Providers reads the class in a
 * passive effect afterwards and agrees with it.
 */

import { useLayoutEffect } from 'react';

export const THEME_STORAGE_KEY = 'techsara.theme';

export function applyStoredTheme(root: HTMLElement = document.documentElement): void {
  if (root.classList.contains('dark') || root.classList.contains('light')) return;
  let theme: 'dark' | 'light' = 'dark';
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') theme = saved;
  } catch {
    // Storage unavailable: the primary theme, as the inline script does.
  }
  root.classList.add(theme);
  root.style.colorScheme = theme;
}

export function StoredTheme() {
  useLayoutEffect(() => {
    applyStoredTheme();
  }, []);
  return null;
}
