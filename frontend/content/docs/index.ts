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
import { audioTranscriptions, audioTranscriptionsPage } from './pages/audioTranscriptions';
import { authentication } from './pages/authentication';
import { background, backgroundPage } from './pages/background';
import { changelog, changelogPage } from './pages/changelog';
import { chatCompletions, chatCompletionsPage } from './pages/chatCompletions';
import { curl, curlPage } from './pages/curl';
import { embeddings, embeddingsPage } from './pages/embeddings';
import { errors, errorsPage } from './pages/errors';
import { fileInputs, fileInputsPage } from './pages/fileInputs';
import { FILES_API_PUBLISHED, files } from './pages/files';
import { idempotency, idempotencyPage } from './pages/idempotency';
import { images } from './pages/images';
import { javascript, javascriptPage } from './pages/javascript';
import { keySecurity } from './pages/keySecurity';
import { NO_TIMEOUT_LIVE, longOutput, longOutputPage } from './pages/longOutput';
import { migration } from './pages/migration';
import { models, modelsPage } from './pages/models';
import { overview } from './pages/overview';
import { python, pythonPage } from './pages/python';
import { quickstart, quickstartPage } from './pages/quickstart';
import { rateLimits, rateLimitsPage } from './pages/rateLimits';
import { rerank, rerankPage } from './pages/rerank';
import { responses, responsesPage } from './pages/responses';
import { security } from './pages/security';
import { status, statusPage } from './pages/status';
import { streaming, streamingPage } from './pages/streaming';
import { timeouts, timeoutsPage } from './pages/timeouts';
import { tools } from './pages/tools';
import { uploads, uploadsPage } from './pages/uploads';
import { usage } from './pages/usage';
import { webhooks } from './pages/webhooks';
import { DEPLOYS_HELD } from './samples';
import type { DocPage, DocSection } from './types';

export type { DocHeading } from './headings';
export { NO_TIMEOUT_LIVE } from './pages/longOutput';
export { headingsOf, slugifyHeading } from './headings';
export type { DocPage, DocSection, ExampleStatus } from './types';
export {
  API_BASE_URL,
  CONSOLE_PATH,
  DEPLOYS_HELD,
  NO_TIMEOUT_EDGE_PROBE,
  deploysHeldOnTheEdge,
  EXAMPLE_KEYS,
  EXAMPLE_LIVE_KEY,
  EXAMPLE_RESPONSE_ID,
  EXAMPLE_TEST_KEY,
  MODEL_ID,
  MODEL_IDS,
  VISION_MODEL_ID,
  OCR_MODEL_ID,
  EMBED_MODEL_ID,
  RERANK_MODEL_ID,
  WHISPER_MODEL_ID,
  LONG_OUTPUT_WALL_CLOCK_LIVE,
  WALL_CLOCK_PENDING_NOTE,
  EXAMPLE_STATUS,
  EXAMPLES_EXECUTED,
  EXECUTED_NOTE,
  NOT_EXECUTED_NOTE,
} from './samples';

/** The page rendered at `/docs` itself, rather than at `/docs/<slug>`. */
export const OVERVIEW_SLUG = overview.slug;

/** The pages whose text depends on the no-timeout release (NO_TIMEOUT_LIVE). */
interface StatePages {
  quickstart: DocPage;
  models: DocPage;
  responses: DocPage;
  chatCompletions: DocPage;
  uploads: DocPage;
  fileInputs: DocPage;
  streaming: DocPage;
  background: DocPage;
  longOutput: DocPage;
  timeouts: DocPage;
  embeddings: DocPage;
  rerank: DocPage;
  audioTranscriptions: DocPage;
  errors: DocPage;
  rateLimits: DocPage;
  idempotency: DocPage;
  python: DocPage;
  javascript: DocPage;
  curl: DocPage;
  changelog: DocPage;
  status: DocPage;
}

function sectionsOf(
  p: StatePages,
  { noTimeout, filesPublished }: { noTimeout: boolean; filesPublished: boolean },
): DocSection[] {
  return [
    {
      title: 'Getting started',
      summary: 'What the platform is, and a first answer out of it.',
      pages: [overview, p.quickstart, authentication, keySecurity],
    },
    {
      title: 'API reference',
      summary: 'Every endpoint, field, event and error code.',
      pages: [
        p.models,
        p.responses,
        p.chatCompletions,
        images,
        // The Files API pages, listed only once its routes are mounted and in
        // CONTRACT §7 (pages/files.ts, FILES_API_PUBLISHED).
        ...(filesPublished ? [files, p.uploads, p.fileInputs] : []),
        p.streaming,
        p.background,
        p.longOutput,
        // 2026-09-13: the timeouts page describes the no-timeout release and
        // nothing else, so it is published with it (pages/longOutput.ts,
        // NO_TIMEOUT_LIVE) — never before.
        ...(noTimeout ? [p.timeouts] : []),
        p.embeddings,
        p.rerank,
        p.audioTranscriptions,
        webhooks,
        p.errors,
        p.rateLimits,
        p.idempotency,
        usage,
        tools,
      ],
    },
    {
      title: 'Examples',
      summary: 'Working code in the three places people start from.',
      pages: [p.python, p.javascript, p.curl],
    },
    {
      title: 'Reference',
      summary: 'Porting, security, what changed, and how to tell if we are up.',
      pages: [migration, security, p.changelog, p.status],
    },
  ];
}

export const DOC_SECTIONS: DocSection[] = sectionsOf(
  {
    quickstart,
    models,
    responses,
    chatCompletions,
    uploads,
    fileInputs,
    streaming,
    background,
    longOutput,
    timeouts,
    embeddings,
    rerank,
    audioTranscriptions,
    errors,
    rateLimits,
    idempotency,
    python,
    javascript,
    curl,
    changelog,
    status,
  },
  { noTimeout: NO_TIMEOUT_LIVE, filesPublished: FILES_API_PUBLISHED },
);

/**
 * The whole site as it will read in a given state, built fresh — for the
 * tests that check the no-timeout pages (links, routes, event names) before
 * the release is live. The site itself reads DOC_SECTIONS.
 */
export function docSectionsFor({
  noTimeout,
  filesPublished = FILES_API_PUBLISHED,
  deploysHeld = DEPLOYS_HELD,
}: {
  noTimeout: boolean;
  filesPublished?: boolean;
  /** Whether the release pages may say deploys are invisible (samples.ts). */
  deploysHeld?: boolean;
}): DocSection[] {
  const state = { noTimeout };
  const held = { noTimeout, deploysHeld };
  return sectionsOf(
    {
      quickstart: quickstartPage(state),
      models: modelsPage(state),
      responses: responsesPage(state),
      chatCompletions: chatCompletionsPage(state),
      uploads: uploadsPage(state),
      fileInputs: fileInputsPage(state),
      streaming: streamingPage(held),
      background: backgroundPage(state),
      longOutput: longOutputPage(held),
      timeouts: timeoutsPage({ filesPublished, deploysHeld }),
      embeddings: embeddingsPage(state),
      rerank: rerankPage(state),
      audioTranscriptions: audioTranscriptionsPage(state),
      errors: errorsPage(state),
      rateLimits: rateLimitsPage(state),
      idempotency: idempotencyPage(state),
      python: pythonPage(state),
      javascript: javascriptPage(state),
      curl: curlPage(state),
      changelog: changelogPage(held),
      status: statusPage(held),
    },
    { noTimeout, filesPublished },
  );
}

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
