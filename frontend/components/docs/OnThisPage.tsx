'use client';

/**
 * The right-hand rail: the `##` and `###` headings of the page being read.
 *
 * Built by `docHeadingsOf` from the same remark parse the renderer uses, and
 * the ids come from the same `slugifyHeading` the renderer stamps onto the
 * elements — which is the only reason these links land anywhere. Two slug implementations would give
 * a contents list that looks right and scrolls nowhere, and nothing but a
 * click would ever notice.
 *
 * Hidden below `xl` rather than stacked: on a narrow screen the page's own
 * headings are a few scrolls away, and a second nav above the article is a
 * second thing to scroll past.
 */

import type { DocHeading } from '@/content/docs';

export function OnThisPage({ headings }: { headings: DocHeading[] }) {
  if (headings.length < 2) return null;
  return (
    <nav aria-label="On this page" className="text-sm">
      <h2 className="pb-2 text-[11px] font-semibold uppercase tracking-wider text-faint">
        On this page
      </h2>
      <ul className="space-y-1 border-l border-border">
        {headings.map((heading) => (
          <li key={heading.id}>
            <a
              href={`#${heading.id}`}
              className={`-ml-px block border-l border-transparent py-0.5 text-muted transition-colors duration-ts hover:border-accent hover:text-ink ${
                heading.depth === 3 ? 'pl-6' : 'pl-3'
              }`}
            >
              {heading.text}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
