import type { Metadata } from 'next';
import { notFound, permanentRedirect } from 'next/navigation';

import { DocsArticle } from '@/components/docs/DocsArticle';
import { DOC_PAGES, OVERVIEW_SLUG, docHref, findDocPage } from '@/content/docs';

/**
 * /docs/<slug> — one documentation page from the registry.
 *
 * Every page is generated from `DOC_PAGES`, which is also what the sidebar
 * and the tests read. A slug that is not in the registry is a 404 rather
 * than an empty shell: documentation that renders a blank page for a typo is
 * how a stale link survives for a year.
 *
 * The overview is excluded here because it lives at `/docs` itself; serving
 * it at both paths would give the same prose two URLs and two anchors for
 * everything on it.
 */
export function generateStaticParams(): { slug: string }[] {
  return DOC_PAGES.filter((page) => page.slug !== OVERVIEW_SLUG).map((page) => ({
    slug: page.slug,
  }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const page = findDocPage(slug);
  if (!page) return { title: 'Not found' };
  return { title: page.title, description: page.summary };
}

export default async function DocsPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  // The overview's own slug, typed as a path, goes where the overview lives
  // instead of a 404 (it was a dead end: `/docs/overview` is the obvious
  // guess for the page every other slug sits beside). One URL still serves it.
  if (slug === OVERVIEW_SLUG) permanentRedirect('/docs');
  const page = findDocPage(slug);
  if (!page) {
    // `/docs/ERRORS` is a typo of a real page, not a missing one.
    const lower = slug.toLowerCase();
    if (lower !== slug && findDocPage(lower)) permanentRedirect(docHref(lower));
    notFound();
  }
  return <DocsArticle page={page} />;
}
