import type { Metadata } from 'next';
import { notFound } from 'next/navigation';

import { DocsArticle } from '@/components/docs/DocsArticle';
import { DOC_PAGES, OVERVIEW_SLUG, findDocPage } from '@/content/docs';

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
  const page = slug === OVERVIEW_SLUG ? undefined : findDocPage(slug);
  if (!page) notFound();
  return <DocsArticle page={page} />;
}
