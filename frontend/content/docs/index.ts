/**
 * The documentation site's table of contents (CONTRACT §17).
 *
 * ONE ordered list of sections, built from the page records themselves. The
 * sidebar, the "next page" link at the foot of every page, the static params
 * Next generates the routes from, and the tests all read THIS — so a page
 * that is written and not listed here does not half-exist, it simply is not
 * published, and the test that walks every internal link catches the first
 * link that points at it.
 */
import { authentication } from './pages/authentication';
import { background } from './pages/background';
import { changelog } from './pages/changelog';
import { chatCompletions } from './pages/chatCompletions';
import { curl } from './pages/curl';
import { errors } from './pages/errors';
import { idempotency } from './pages/idempotency';
import { javascript } from './pages/javascript';
import { keySecurity } from './pages/keySecurity';
import { migration } from './pages/migration';
import { models } from './pages/models';
import { overview } from './pages/overview';
import { python } from './pages/python';
import { quickstart } from './pages/quickstart';
import { rateLimits } from './pages/rateLimits';
import { responses } from './pages/responses';
import { security } from './pages/security';
import { status } from './pages/status';
import { streaming } from './pages/streaming';
import { tools } from './pages/tools';
import { usage } from './pages/usage';
import { webhooks } from './pages/webhooks';
import type { DocPage, DocSection } from './types';

export type { DocHeading } from './headings';
export { headingsOf, slugifyHeading } from './headings';
export type { DocPage, DocSection, ExampleStatus } from './types';
export {
  API_BASE_URL,
  CONSOLE_PATH,
  EXAMPLE_KEYS,
  EXAMPLE_LIVE_KEY,
  EXAMPLE_RESPONSE_ID,
  EXAMPLE_TEST_KEY,
  MODEL_ID,
  EXAMPLE_STATUS,
  EXAMPLES_EXECUTED,
  EXECUTED_NOTE,
  NOT_EXECUTED_NOTE,
} from './samples';

/** The page rendered at `/docs` itself, rather than at `/docs/<slug>`. */
export const OVERVIEW_SLUG = overview.slug;

export const DOC_SECTIONS: DocSection[] = [
  {
    title: 'Getting started',
    summary: 'What the platform is, and a first answer out of it.',
    pages: [overview, quickstart, authentication, keySecurity],
  },
  {
    title: 'API reference',
    summary: 'Every endpoint, field, event and error code.',
    pages: [
      models,
      responses,
      chatCompletions,
      streaming,
      background,
      webhooks,
      errors,
      rateLimits,
      idempotency,
      usage,
      tools,
    ],
  },
  {
    title: 'Examples',
    summary: 'Working code in the three places people start from.',
    pages: [python, javascript, curl],
  },
  {
    title: 'Reference',
    summary: 'Porting, security, what changed, and how to tell if we are up.',
    pages: [migration, security, changelog, status],
  },
];

/** Every page, in sidebar order. */
export const DOC_PAGES: DocPage[] = DOC_SECTIONS.flatMap((section) => section.pages);

/**
 * The URL for a page. The overview lives at `/docs`, not `/docs/overview`,
 * because a section index that redirects to its own first child is a link
 * nobody can copy confidently.
 */
export function docHref(slug: string): string {
  return slug === OVERVIEW_SLUG ? '/docs' : `/docs/${slug}`;
}

export function findDocPage(slug: string): DocPage | undefined {
  return DOC_PAGES.find((page) => page.slug === slug);
}

/** The section a page belongs to, by its own `section` title. */
export function sectionOf(page: DocPage): DocSection | undefined {
  return DOC_SECTIONS.find((section) => section.title === page.section);
}

export interface DocNeighbours {
  previous?: DocPage;
  next?: DocPage;
}

/**
 * The pages either side of this one in reading order — the "keep going" link
 * at the foot of a page. Documentation is read in order far more often than
 * anyone building it expects.
 */
export function neighboursOf(slug: string): DocNeighbours {
  const index = DOC_PAGES.findIndex((page) => page.slug === slug);
  if (index < 0) return {};
  return {
    previous: index > 0 ? DOC_PAGES[index - 1] : undefined,
    next: index < DOC_PAGES.length - 1 ? DOC_PAGES[index + 1] : undefined,
  };
}
