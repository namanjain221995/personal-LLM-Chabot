'use client';

/**
 * One documentation page: its title, the honesty notice about its examples,
 * the prose, and the way onward.
 *
 * The "examples not executed" banner is not decoration. CONTRACT §17 says an
 * example that has not been run against the running API is MARKED as not
 * executed rather than presented as verified, and a rule like that only
 * survives if it is rendered from the page's own data — a flag somebody has
 * to flip — instead of being a paragraph an author remembers to write.
 */

import Link from 'next/link';
import { useMemo } from 'react';
import { DocsMarkdown } from './DocsMarkdown';
import { OnThisPage } from './OnThisPage';
import { docHeadingsOf } from './docHeadings';
import type { DocPage } from '@/content/docs';
import { docHref, neighboursOf } from '@/content/docs';

/**
 * The notice's look, per state.
 *
 * 2026-09-13, verifier finding: this used the `warn` colour with `/40` and
 * `/10` opacity modifiers, and tailwind.config.ts declares `warn` as a bare
 * `var(--ts-warn)`, so those modifiers compiled to NO RULE AT ALL. All 22 pages rendered the one
 * element CONTRACT §17 mandates as ordinary prose in a grey hairline box.
 * jsdom loads no CSS, so no DOM test could see it.
 *
 * The fix uses only utilities that compile against the config as it stands
 * (the config and globals.css belong to another owner): an arbitrary
 * `color-mix()` over the theme variable for the tint and the hairline, and a
 * full-strength `border-l-warn` bar. Every colour is still a `--ts-*` token,
 * so html.light re-tints it with no second rule, and the body text stays
 * `text-ink` — full contrast in both themes — because the amber is a signal,
 * not a text colour (the paper theme's amber is under 4.5:1 for 14px text).
 * tests/docs-site.test.tsx compiles these classes with the real Tailwind
 * config and fails if any of them produces nothing.
 */
const NOTICE_TONE = {
  pending:
    'border-[color-mix(in_srgb,var(--ts-warn)_45%,transparent)] border-l-warn ' +
    'bg-[color-mix(in_srgb,var(--ts-warn)_12%,transparent)]',
  executed: 'border-ok/40 border-l-ok bg-ok/10',
  pendingIcon: 'text-warn',
  executedIcon: 'text-ok',
} as const;

function ExampleNotice({ page }: { page: DocPage }) {
  const { executed, note } = page.examples;
  return (
    <div
      // A note, not a live region: it is a standing caveat about the page,
      // present from first paint, and announcing it as a status update on
      // every client navigation would be noise.
      role="note"
      aria-label={executed ? 'Examples verified' : 'Examples not yet executed'}
      data-testid="docs-example-status"
      data-executed={executed ? 'true' : 'false'}
      className={`flex gap-3 rounded-ts border border-l-4 px-4 py-3 text-sm text-ink ${
        executed ? NOTICE_TONE.executed : NOTICE_TONE.pending
      }`}
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 20 20"
        fill="currentColor"
        className={`mt-0.5 h-4 w-4 shrink-0 ${
          executed ? NOTICE_TONE.executedIcon : NOTICE_TONE.pendingIcon
        }`}
      >
        {executed ? (
          <path
            fillRule="evenodd"
            d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.7-9.3a1 1 0 0 0-1.4-1.4L9 10.6 7.7 9.3a1 1 0 0 0-1.4 1.4l2 2a1 1 0 0 0 1.4 0l4-4Z"
            clipRule="evenodd"
          />
        ) : (
          <path
            fillRule="evenodd"
            d="M8.3 2.9a2 2 0 0 1 3.4 0l6.1 10.6A2 2 0 0 1 16.1 16.5H3.9a2 2 0 0 1-1.7-3L8.3 2.9ZM10 7a1 1 0 0 0-1 1v3a1 1 0 1 0 2 0V8a1 1 0 0 0-1-1Zm0 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z"
            clipRule="evenodd"
          />
        )}
      </svg>
      <p className="m-0">
        <span className="font-semibold">
          {executed ? 'Examples verified. ' : 'Examples not yet executed. '}
        </span>
        {note}
      </p>
    </div>
  );
}

export function DocsArticle({ page }: { page: DocPage }) {
  // From the same remark parse the renderer uses, so a rail id is always an
  // element id (see docHeadings.ts for the two headings that proved otherwise).
  const headings = useMemo(() => docHeadingsOf(page.body), [page.body]);
  const { previous, next } = neighboursOf(page.slug);

  return (
    <div className="flex w-full gap-10">
      <article className="min-w-0 flex-1">
        <header className="mb-6">
          <p className="text-xs font-semibold uppercase tracking-wider text-accent">
            {page.section}
          </p>
          <h1 className="mt-1 text-xl font-semibold text-ink">{page.title}</h1>
          <p className="mt-2 text-base text-muted">{page.summary}</p>
        </header>

        <div className="mb-8">
          <ExampleNotice page={page} />
        </div>

        <DocsMarkdown body={page.body} />

        {(previous || next) && (
          <nav
            aria-label="Page navigation"
            className="mt-12 grid gap-3 border-t border-border pt-6 sm:grid-cols-2"
          >
            {previous ? (
              <Link
                href={docHref(previous.slug)}
                className="rounded-ts border border-border bg-surface p-4 no-underline transition-colors duration-ts hover:border-accent/50"
              >
                <span className="block text-xs uppercase tracking-wide text-faint">
                  Previous
                </span>
                <span className="mt-1 block text-sm font-medium text-ink">
                  {previous.title}
                </span>
              </Link>
            ) : (
              <span />
            )}
            {next && (
              <Link
                href={docHref(next.slug)}
                className="rounded-ts border border-border bg-surface p-4 text-right no-underline transition-colors duration-ts hover:border-accent/50 sm:col-start-2"
              >
                <span className="block text-xs uppercase tracking-wide text-faint">
                  Next
                </span>
                <span className="mt-1 block text-sm font-medium text-ink">
                  {next.title}
                </span>
              </Link>
            )}
          </nav>
        )}
      </article>

      {/* The contents rail. Wide screens only: below xl it would push the
          prose narrower than it can afford to be. */}
      <aside className="hidden w-56 shrink-0 xl:block">
        <div className="sticky top-24">
          <OnThisPage headings={headings} />
        </div>
      </aside>
    </div>
  );
}
