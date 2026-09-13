// @vitest-environment jsdom
/**
 * The documentation site below lg and at its edges (responsive audit,
 * 2026-09-13): a drawer that no longer covers its own Close button, focus
 * that moves into it, tables whose prose wraps, and wrong URLs that stay
 * inside the documentation.
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { ComponentProps, ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const navigation = vi.hoisted(() => ({
  permanentRedirect: vi.fn((url: string) => {
    throw new Error(`redirect:${url}`);
  }),
  notFound: vi.fn(() => {
    throw new Error('notFound');
  }),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => '/docs/errors',
  permanentRedirect: navigation.permanentRedirect,
  notFound: navigation.notFound,
}));

vi.mock('next/link', () => ({
  __esModule: true,
  default: (props: ComponentProps<'a'> & { children?: ReactNode }) => {
    const { children, ...rest } = props;
    return <a {...rest}>{children}</a>;
  },
}));

import DocsSlugPage from '@/app/docs/[slug]/page';
import DocsNotFound from '@/app/docs/not-found';
import { DocsMarkdown } from '@/components/docs/DocsMarkdown';
import { DocsShell } from '@/components/docs/DocsShell';
import { DOC_SECTIONS, OVERVIEW_SLUG, docHref } from '@/content/docs';

afterEach(() => {
  cleanup();
  document.body.style.overflow = '';
  navigation.permanentRedirect.mockClear();
  navigation.notFound.mockClear();
});

const classes = (el: Element | null) => (el?.getAttribute('class') ?? '').split(/\s+/);

describe('the docs drawer', () => {
  it('opens under the header, so the Close button it replaces stays reachable', () => {
    render(
      <DocsShell>
        <p>page body</p>
      </DocsShell>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }));
    const panel = document.getElementById('docs-nav-panel') as HTMLElement;
    // It was `fixed inset-y-0 … z-40`: drawn from the top of the window,
    // over the z-30 sticky header and the toggle inside it.
    expect(classes(panel)).toEqual(expect.arrayContaining(['fixed', 'top-14', 'bottom-0']));
    expect(classes(panel)).not.toContain('inset-y-0');
    const scrim = document.querySelector('[aria-hidden="true"].fixed') as HTMLElement;
    expect(classes(scrim)).toContain('top-14');
    expect(classes(scrim)).not.toContain('inset-0');

    // Focus moves into the drawer, and the page behind stops scrolling.
    expect(panel.contains(document.activeElement)).toBe(true);
    expect(document.body.style.overflow).toBe('hidden');

    fireEvent.click(within(screen.getByRole('banner')).getByRole('button', { name: 'Close' }));
    expect(screen.getByRole('button', { name: 'Menu' }).getAttribute('aria-expanded')).toBe('false');
    expect(document.body.style.overflow).toBe('');
  });
});

describe('documentation tables', () => {
  it('render inside the docs-only prose class whose cells wrap', () => {
    const { container } = render(
      <DocsMarkdown body={'| Code | Meaning |\n| --- | --- |\n| 409 | A long explanation that must wrap |\n'} />,
    );
    const prose = container.querySelector('.md') as HTMLElement;
    expect(classes(prose)).toContain('md-docs');
    expect(prose.querySelector('.md-table-wrap table')).toBeTruthy();
  });
});

describe('a documentation URL that is not a page', () => {
  it('sends /docs/overview to /docs, where the overview lives', async () => {
    await expect(DocsSlugPage({ params: Promise.resolve({ slug: OVERVIEW_SLUG }) })).rejects.toThrow(
      'redirect:/docs',
    );
  });

  it('sends a wrongly-cased slug to the real page', async () => {
    const real = DOC_SECTIONS.flatMap((s) => s.pages).find((p) => p.slug !== OVERVIEW_SLUG)!;
    await expect(
      DocsSlugPage({ params: Promise.resolve({ slug: real.slug.toUpperCase() }) }),
    ).rejects.toThrow(`redirect:${docHref(real.slug)}`);
  });

  it('is a 404 for a slug that is nothing', async () => {
    await expect(
      DocsSlugPage({ params: Promise.resolve({ slug: 'does-not-exist' }) }),
    ).rejects.toThrow('notFound');
    expect(navigation.permanentRedirect).not.toHaveBeenCalled();
  });

  it('renders a not-found page that leads back into the documentation', () => {
    render(<DocsNotFound />);
    expect(screen.getByRole('heading', { name: 'This page is not in the documentation' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Back to the documentation home' }).getAttribute('href')).toBe(
      '/docs',
    );
    const first = DOC_SECTIONS[0].pages[0];
    expect(document.querySelector(`a[href="${docHref(first.slug)}"]`)).toBeTruthy();
  });
});
