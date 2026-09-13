// @vitest-environment jsdom
/**
 * The application-level 404 (app/not-found.tsx).
 *
 * It replaced Next's bare "404 This page could not be found." — no theme, no
 * way back — for unknown URLs and for every notFound() outside the docs,
 * including the developer console refusing a member (re-audit, 2026-09-13).
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { cleanup, render, screen, within } from '@testing-library/react';
import type { ComponentProps, ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  __esModule: true,
  default: (props: ComponentProps<'a'> & { children?: ReactNode }) => {
    const { children, ...rest } = props;
    return <a {...rest}>{children}</a>;
  },
}));

import DocsNotFound from '@/app/docs/not-found';
import NotFound from '@/app/not-found';
import { applyStoredTheme } from '@/components/StoredTheme';
import { authRedirect, isStaticAssetPath } from '@/lib/auth';

afterEach(() => {
  cleanup();
  document.documentElement.className = '';
  document.documentElement.style.colorScheme = '';
  localStorage.clear();
});

describe('the application 404', () => {
  it('says the page is missing and offers the way back to chat and to the docs', () => {
    render(<NotFound />);
    expect(screen.getByRole('heading', { level: 1, name: 'This page could not be found' })).toBeTruthy();
    const ways = screen.getByRole('navigation', { name: 'Ways back' });
    expect(within(ways).getByRole('link', { name: 'Back to chat' }).getAttribute('href')).toBe('/');
    expect(within(ways).getByRole('link', { name: 'API documentation' }).getAttribute('href')).toBe('/docs');
  });

  it('never mentions or links the console, whose refusal it also renders', () => {
    // CONTRACT §6: /api answers a member 404 so as not to disclose that it
    // exists. A 404 that said "you lack console access" would undo that.
    const { container } = render(<NotFound />);
    expect(container.querySelector('a[href^="/api"]')).toBeNull();
    expect(container.textContent ?? '').not.toMatch(/console|access/i);
  });

  it('paints with the theme tokens, and gives each way back a 40px target', () => {
    const { container } = render(<NotFound />);
    const main = container.querySelector('main') as HTMLElement;
    expect(main.className).toMatch(/(^|\s)bg-bg(\s|$)/);
    expect(main.className).toMatch(/(^|\s)text-ink(\s|$)/);
    for (const link of screen.getAllByRole('link')) {
      expect(link.className).toMatch(/(^|\s)min-h-10(\s|$)/);
    }
  });
});

describe('the /favicon.ico a not-found page can trigger', () => {
  it('is a real icon file, served past the sign-in gate', () => {
    // Chrome falls back to /favicon.ico on some not-found renders even with
    // the metadata icon in <head>; without the file that was a second 404 in
    // the console of the page that already had one.
    const ico = readFileSync(join(process.cwd(), 'public', 'favicon.ico'));
    // ICONDIR: reserved 0, type 1 (icon), at least one image.
    expect(ico.readUInt16LE(0)).toBe(0);
    expect(ico.readUInt16LE(2)).toBe(1);
    expect(ico.readUInt16LE(4)).toBeGreaterThanOrEqual(1);
    expect(isStaticAssetPath('/favicon.ico')).toBe(true);
    expect(authRedirect('/favicon.ico', false)).toBeNull();
  });
});

describe('a 404 rendered in the browser, where the theme script never ran', () => {
  // A thrown notFound() gets Next's error shell and a client render; the root
  // layout's inline theme script is inserted by React and never executes, so
  // both 404 pages drew dark for a reader who had chosen light.
  it('applies the saved light theme when rendering either not-found page', () => {
    localStorage.setItem('techsara.theme', 'light');
    render(<NotFound />);
    expect(document.documentElement.classList.contains('light')).toBe(true);
    expect(document.documentElement.style.colorScheme).toBe('light');
    cleanup();
    document.documentElement.className = '';
    render(<DocsNotFound />);
    expect(document.documentElement.classList.contains('light')).toBe(true);
  });

  it('falls back to dark with nothing saved, as the inline script does', () => {
    applyStoredTheme();
    expect(document.documentElement.className).toBe('dark');
  });

  it('leaves a theme the script (or the toggle) already set alone', () => {
    document.documentElement.classList.add('dark');
    localStorage.setItem('techsara.theme', 'light');
    applyStoredTheme();
    expect(document.documentElement.className).toBe('dark');
  });
});
