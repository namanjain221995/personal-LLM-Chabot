import type { Metadata } from 'next';

import { DocsArticle } from '@/components/docs/DocsArticle';
import { OVERVIEW_SLUG, findDocPage } from '@/content/docs';

/**
 * /docs — the landing page.
 *
 * The overview is a page in the same registry as every other, rendered at
 * `/docs` rather than `/docs/overview` so the documentation's front door is
 * a URL someone can paste without wondering whether they got the redirect.
 * `findDocPage` cannot miss here — the overview is the first entry of the
 * first section — but the non-null assertion is worth avoiding: a registry
 * edit that dropped it should fail the build, not render an empty page.
 */
const overview = findDocPage(OVERVIEW_SLUG);

export const metadata: Metadata = {
  title: 'TechSara API documentation',
  description: overview?.summary,
};

export default function DocsHomePage() {
  if (!overview) {
    // The detail names a source file, so it goes to the server log and the
    // thrown message stays generic. Next masks a server component's message
    // in production anyway; this keeps a dev build or a misconfigured one
    // from printing a path to whoever loaded the page (2026-09-13 review).
    console.error(
      `the documentation registry has no "${OVERVIEW_SLUG}" page; ` +
        'content/docs/index.ts must list it',
    );
    throw new Error('The documentation is unavailable.');
  }
  return <DocsArticle page={overview} />;
}
