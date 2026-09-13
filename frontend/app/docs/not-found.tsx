import Link from 'next/link';

import { StoredTheme } from '@/components/StoredTheme';
import { DOC_SECTIONS, docHref } from '@/content/docs';

/**
 * A missing documentation page, inside the documentation.
 *
 * `/docs/<unknown>` used to fall through to Next's bare white 404: no docs
 * header, no sidebar, no way back in, and the same page in both themes. This
 * renders inside the docs layout (DocsShell), so the header and the table of
 * contents are still there, and it offers the first page of every section.
 *
 * <StoredTheme />: notFound() thrown by the slug page makes Next render this
 * in the browser, where the root layout's theme script never runs — the page
 * was dark for a reader who had chosen light (components/StoredTheme).
 */
export default function DocsNotFound() {
  return (
    <div className="max-w-2xl py-8">
      <StoredTheme />
      <p className="text-sm font-medium text-muted">404</p>
      <h1 className="mt-2 text-xl font-semibold text-ink">
        This page is not in the documentation
      </h1>
      <p className="mt-3 text-base text-muted">
        The link may be out of date, or the address mistyped. Every page is in
        the menu; these are good places to start.
      </p>
      <ul className="mt-6 grid gap-2 sm:grid-cols-2">
        {DOC_SECTIONS.filter((section) => section.pages.length > 0).map((section) => (
          <li key={section.title}>
            <Link
              href={docHref(section.pages[0].slug)}
              className="block min-h-10 rounded-ts border border-border bg-surface px-4 py-3 no-underline transition-colors duration-ts hover:bg-surface-2"
            >
              <span className="block text-xs uppercase tracking-wide text-muted">
                {section.title}
              </span>
              <span className="mt-1 block text-sm font-medium text-ink">
                {section.pages[0].title}
              </span>
            </Link>
          </li>
        ))}
      </ul>
      <p className="mt-6 text-sm">
        <Link href="/docs" className="text-accent">
          Back to the documentation home
        </Link>
      </p>
    </div>
  );
}
