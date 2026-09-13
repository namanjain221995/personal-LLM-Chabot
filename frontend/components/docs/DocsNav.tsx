'use client';

/**
 * The documentation sidebar: every section, every page, always.
 *
 * NOTHING IS HIDDEN HERE. The whole table of contents renders on every page,
 * because a documentation nav that collapses to the current section makes the
 * reader click to discover what exists — and the thing a first-time reader
 * most needs is the shape of the whole surface.
 *
 * `currentHref` is a PROP rather than a `usePathname()` call inside this
 * component, so the nav can be rendered and asserted without a Next router.
 * The shell does the routing; this draws a list.
 */

import Link from 'next/link';
import type { DocSection } from '@/content/docs';
import { docHref } from '@/content/docs';
import { slugifyHeading } from '@/content/docs/headings';

export function DocsNav({
  sections,
  currentHref,
  onNavigate,
  idPrefix = 'docs-nav',
}: {
  sections: DocSection[];
  /** The path of the page being read, e.g. `/docs/errors`. */
  currentHref: string;
  /** Called after a link is followed — the mobile drawer closes itself. */
  onNavigate?: () => void;
  /**
   * Prefix for the section heading ids `aria-labelledby` points at. It is a
   * prop because rendering this list twice — a desktop column and a mobile
   * drawer — would otherwise put the same id in the document twice, and
   * every label would resolve to whichever copy came first. That exact bug
   * (M-12, the chat sidebar) is why it is a prop and not a literal.
   */
  idPrefix?: string;
}) {
  return (
    <nav aria-label="Documentation" className="text-sm">
      <ul className="space-y-6">
        {sections.map((section) => {
          const headingId = `${idPrefix}-${slugifyHeading(section.title)}`;
          return (
            <li key={section.title}>
              <h2
                id={headingId}
                className="px-3 pb-2 text-[11px] font-semibold uppercase tracking-wider text-faint"
              >
                {section.title}
              </h2>
              <ul aria-labelledby={headingId} className="space-y-0.5">
                {section.pages.map((page) => {
                  const href = docHref(page.slug);
                  const current = href === currentHref;
                  return (
                    <li key={page.slug}>
                      <Link
                        href={href}
                        onClick={onNavigate}
                        // aria-current is the accessible half of the
                        // highlight: the colour alone tells a screen-reader
                        // user nothing about where they are.
                        aria-current={current ? 'page' : undefined}
                        className={`block rounded-md px-3 py-1.5 transition-colors duration-ts ${
                          current
                            ? 'bg-accent/10 font-medium text-accent'
                            : 'text-muted hover:bg-surface-2 hover:text-ink'
                        }`}
                      >
                        {page.title}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
