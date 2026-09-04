// @vitest-environment jsdom
/**
 * NEW-24 / M-08 / M-09 — what a streaming token is allowed to COST the view.
 *
 * The bug these lock down was not a wrong pixel, it was work: a 100-message
 * thread re-rendered all 100 rows on every delta (measured 50,200 row renders
 * for one 500-delta answer), rebuilt the branch tree twice per delta, and
 * forced a synchronous layout for the auto-scroll each time. Nothing about
 * the OUTPUT was wrong, so only counters can catch a regression here.
 *
 * Deterministic by construction: `requestAnimationFrame` is a manual queue,
 * so "a display frame" is a function call and no assertion depends on timing.
 */

import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import type { ChatMessage } from '@/lib/types';

const HISTORY = 100;

const counters = { treeWalks: 0, scrollWrites: 0, markdownParses: 0, markdownChars: 0 };

// Every actual react-markdown invocation, and how much text it was handed.
// This is the metric NEW-24 is really about: before segmentation it was one
// parse of the WHOLE answer per frame, and remark-parse is superlinear.
vi.mock('react-markdown', async (orig) => {
  const actual = (await orig()) as { default: unknown };
  const Real = actual.default as (p: { children?: string }) => unknown;
  return {
    ...actual,
    default: (props: { children?: string }) => {
      counters.markdownParses += 1;
      counters.markdownChars += (props.children ?? '').length;
      return Real(props);
    },
  };
});

const history = Array.from({ length: HISTORY }, (_, i) => ({
  id: `h${i}`,
  role: i % 2 === 0 ? 'user' : 'assistant',
  content: `Historical message ${i}.`,
  status: 'done',
  createdAt: 1000 + i,
})) as ChatMessage[];

/**
 * Props handed to each row, by message id, on the most recent render.
 *
 * The wrapper here is deliberately NOT memoized: wrapping the real row in a
 * memo of the test's own would short-circuit BEFORE the component's, and the
 * test would then pass with the real memo deleted (it did, first time round).
 * So this records what the parent passed down, and the assertions below prove
 * the two independent facts that together mean a historical row cannot
 * re-render: its props are referentially identical frame to frame, and the
 * component is memoized on exactly that comparison.
 */
const propsById = new Map<string, Record<string, unknown>>();
const unstable = new Map<string, Set<string>>();

vi.mock('@/components/MessageRow', async (orig) => {
  const actual = (await orig()) as typeof import('@/components/MessageRow');
  const { createElement } = await import('react');
  const Real = actual.MessageRow;
  return {
    ...actual,
    MessageRow: (props: Record<string, unknown>) => {
      const id = String((props.message as ChatMessage).id);
      const previous = propsById.get(id);
      if (previous) {
        for (const key of Object.keys(props)) {
          if (!Object.is(previous[key], props[key])) {
            const set = unstable.get(id) ?? new Set<string>();
            set.add(key);
            unstable.set(id, set);
          }
        }
      }
      propsById.set(id, props);
      return createElement(Real as never, props as never);
    },
  };
});

// Spied at the MODULE BOUNDARY — the two derivations ChatApp itself calls.
// (Spying `buildTree` would prove nothing: `threadIndices` and `versionMap`
// reach it through a module-internal reference the mock cannot intercept, so
// the counter would read zero however often the tree was rebuilt.)
vi.mock('@/lib/branching', async (orig) => {
  const actual = (await orig()) as typeof import('@/lib/branching');
  return {
    ...actual,
    threadIndices: (...a: Parameters<typeof actual.threadIndices>) => {
      counters.treeWalks += 1;
      return actual.threadIndices(...a);
    },
    versionMap: (...a: Parameters<typeof actual.versionMap>) => {
      counters.treeWalks += 1;
      return actual.versionMap(...a);
    },
  };
});

const conversation = { id: 'conv-1', title: 'Streaming chat', messages: history, createdAt: 0, updatedAt: 0 };
const other = { id: 'conv-2', title: 'Other chat', messages: [
  { id: 'o1', role: 'user', content: 'Unrelated question', status: 'done', createdAt: 1 },
] as ChatMessage[], createdAt: 0, updatedAt: 0 };
const byId: Record<string, typeof conversation> = { 'conv-1': conversation, 'conv-2': other };
vi.mock('@/lib/history', () => ({
  newId: () => `m${Math.random().toString(36).slice(2, 10)}`,
  setEvictListener: () => undefined,
  rebuildHistoryStore: async () => { throw new Error('unexpected account switch'); },
  getHistoryStore: () => ({
    ready: async () => undefined,
    list: () => [
      { id: 'conv-1', title: 'Streaming chat', createdAt: 0, updatedAt: 2 },
      { id: 'conv-2', title: 'Other chat', createdAt: 0, updatedAt: 1 },
    ],
    listArchived: () => [],
    get: (id: string) => byId[id] ?? null,
    create: (title: string) => ({ id: 'conv-1', title, messages: [], createdAt: 0, updatedAt: 0 }),
    saveMessages: () => undefined,
    load: async (id: string) => byId[id] ?? null,
    setActiveUser: () => false,
    wipeLocal: async () => undefined,
    migrateLocalConversations: async () => 0,
    refresh: async () => true,
    refreshArchived: async () => true,
    generateTitle: async () => undefined,
    truncateMessages: async () => undefined,
    setMessageFeedback: async () => undefined,
    exportMarkdown: async () => null,
    remove: () => undefined,
    rename: () => undefined,
    setPinned: () => undefined,
    setArchived: () => undefined,
  }),
}));
vi.mock('@/lib/auth', () => ({
  fetchMe: async () => ({ ok: true, username: 't', user: null, features: {} }),
  userScopeKey: () => 't',
  redirectToLogin: () => undefined,
  handleSessionEnd: () => undefined,
}));
vi.mock('@/lib/salesforceApi', () => ({
  fetchSalesforceContext: async () => ({ options: [], pending: null }),
  cancelClarification: async () => undefined,
  shouldShowStarter: () => false,
}));
vi.mock('@/lib/compact', () => ({ isCompacting: () => false, requestCompact: async () => null }));

let frames: (FrameRequestCallback | null)[] = [];
function paint() {
  const due = frames;
  frames = [];
  for (const cb of due) if (cb) cb(0);
}

let enqueue: ((chunk: string) => void) | null = null;
let finish: (() => void) | null = null;

beforeEach(() => {
  Object.keys(counters).forEach((k) => { (counters as Record<string, number>)[k] = 0; });
  propsById.clear();
  unstable.clear();
  frames = [];
  enqueue = null;
  finish = null;
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    frames.push(cb);
    return frames.length;
  });
  vi.stubGlobal('cancelAnimationFrame', (h: number) => { frames[h - 1] = null; });
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: () => undefined, removeEventListener: () => undefined,
    addListener: () => undefined, removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => undefined);
  Object.defineProperty(Element.prototype, 'scrollTop', {
    configurable: true,
    get: () => 0,
    set: () => { counters.scrollWrites += 1; },
  });
  window.history.replaceState({}, '', '/?c=conv-1');
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    const u = String(url);
    if (u === '/api/chat/active') return { ok: true, status: 200, json: async () => ({ active: [] }) };
    if (u.startsWith('/api/chat')) {
      const encoder = new TextEncoder();
      return {
        ok: true, status: 200,
        body: new ReadableStream<Uint8Array>({
          start(c) {
            enqueue = (chunk) => c.enqueue(encoder.encode(chunk));
            finish = () => c.close();
          },
        }),
      };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // The stream registry is module state; a fresh copy per test keeps one
  // test's live generation out of the next one's counters.
  vi.resetModules();
});

const token = (text: string) => `event: token\ndata: ${JSON.stringify({ text })}\n\n`;

/** Mount ChatApp on the seeded conversation and open a stream on it. */
async function mountStreaming() {
  const { ChatApp } = await import('@/components/ChatApp');
  const { Providers } = await import('@/components/Providers');
  const { startStream } = await import('@/lib/streams');
  const view = render(<Providers><ChatApp /></Providers>);
  await waitFor(() =>
    expect(document.querySelectorAll('[data-chat-message-role]').length).toBe(HISTORY),
  );
  const running = act(async () => {
    void startStream({
      conversationId: 'conv-1',
      turns: [
        ...history,
        { id: 'u-new', role: 'user', content: 'Ask', status: 'done', createdAt: 9 } as ChatMessage,
      ],
      prefs: {
        model: 'fast', effort: 'low', agent: false, webSearch: false,
        deepResearch: false, salesforce: false, sfLive: false,
      } as never,
    });
  });
  await running;
  await waitFor(() => expect(enqueue).not.toBeNull());
  return view;
}

/** Feed one delta and let exactly one display frame elapse. */
async function delta(text: string) {
  await act(async () => { enqueue!(token(text)); await Promise.resolve(); });
  await act(async () => { paint(); await Promise.resolve(); });
}

test('TEST 8: MessageRow is memoized on its props', async () => {
  // The REAL module — this file mocks it, and the mock is a plain wrapper.
  const { MessageRow } = await vi.importActual<
    typeof import('@/components/MessageRow')
  >('@/components/MessageRow');
  // Half of what keeps a historical row off the CPU. Without this, stable
  // props buy nothing — React would re-run the body regardless.
  expect((MessageRow as { $$typeof?: symbol }).$$typeof).toBe(
    Symbol.for('react.memo'),
  );
});

test('TEST 8: an unchanged historical row keeps every prop identical while streaming', async () => {
  await mountStreaming();
  // Everything up to here is mount cost; the stream is what is on trial.
  Object.keys(counters).forEach((k) => { (counters as Record<string, number>)[k] = 0; });
  propsById.clear();
  unstable.clear();

  const DELTAS = 200;
  for (let i = 0; i < DELTAS; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await delta(`word${i} `);
  }

  // The other half. 100 unchanged rows × 200 display updates was 20,000
  // wasted renders, and it was caused by the PROPS: eight inline arrows per
  // row rebuilt on every render, plus a fresh `versions` map. With every prop
  // referentially identical, the memo above provably skips the row — and if
  // any of them starts changing again, this names which one.
  const churned = [...unstable.entries()]
    .filter(([id]) => id.startsWith('h'))
    .map(([id, keys]) => `${id}: ${[...keys].join(', ')}`);
  expect(churned).toEqual([]);
  // Sanity: the rows really were re-visited, so the assertion above is not
  // passing on an empty set.
  expect([...propsById.keys()].filter((id) => id.startsWith('h')).length).toBe(HISTORY);

  // The branch tree is a function of the conversation's SHAPE, which a token
  // does not change. Both derivations used to re-walk it on every delta —
  // two full rebuilds per token, 400 of them across this run.
  expect(counters.treeWalks).toBe(0);
  // One scroll write per frame at most — never several per frame, and never
  // a layout read/write pair per token.
  expect(counters.scrollWrites).toBeLessThanOrEqual(DELTAS);

  await act(async () => { enqueue!('event: done\ndata: {}\n\n'); finish!(); await Promise.resolve(); });
});

test('TEST 2/3 (view): many deltas in one frame commit once, and `done` flushes', async () => {
  await mountStreaming();
  await delta('First. '); // first token commits immediately
  const answerText = () => {
    const rows = document.querySelectorAll('[data-chat-message-role="assistant"]');
    return rows[rows.length - 1]?.textContent ?? '';
  };
  expect(answerText()).toContain('First.');
  counters.scrollWrites = 0;

  // Four deltas with no frame in between: the view must not move yet...
  await act(async () => {
    for (const t of ['A', 'B', 'C', 'D']) enqueue!(token(t));
    await Promise.resolve();
  });
  expect(answerText()).not.toContain('ABCD');
  expect(counters.scrollWrites).toBe(0);

  // ...and then catch up in ONE commit, with all four deltas at once.
  await act(async () => { paint(); await Promise.resolve(); });
  expect(answerText()).toContain('First. ABCD');

  // The follow-scroll books its own frame from the effect that runs after
  // that commit, so the layout write lands on the NEXT frame — one write for
  // the four deltas, not one per delta, and never interleaved with the render
  // that produced them.
  expect(counters.scrollWrites).toBe(0);
  await act(async () => { paint(); await Promise.resolve(); });
  expect(counters.scrollWrites).toBe(1);

  // Pending content that no frame has painted, then `done`.
  await act(async () => { enqueue!(token(' tail')); await Promise.resolve(); });
  await act(async () => {
    enqueue!('event: done\ndata: {}\n\n');
    finish!();
    await Promise.resolve();
  });

  const answer = document.querySelectorAll('[data-chat-message-role="assistant"]');
  expect(answer[answer.length - 1].textContent).toContain('First. ABCD tail');
});

test('TEST 10: streamed markdown renders as markdown, not as source', async () => {
  await mountStreaming();
  const chunks = [
    '# Heading\n\n',
    '**bold**\n\n',
    '- one\n',
    '- two\n\n',
    '```ts\n',
    'const value = 123;\n',
    '```\n',
  ];
  for (const c of chunks) {
    // eslint-disable-next-line no-await-in-loop
    await delta(c);
  }
  await act(async () => { enqueue!('event: done\ndata: {}\n\n'); finish!(); await Promise.resolve(); });

  const rows = document.querySelectorAll('[data-chat-message-role="assistant"]');
  const answer = rows[rows.length - 1] as HTMLElement;
  expect(answer.querySelector('h1')?.textContent).toBe('Heading');
  expect(answer.querySelector('strong')?.textContent).toBe('bold');
  expect(answer.querySelectorAll('li')).toHaveLength(2);
  expect(answer.querySelector('pre')?.textContent).toContain('const value = 123;');
  // The fence is a real code block, not literal backticks in the prose.
  expect(answer.textContent).not.toContain('```');
});

test('TEST 7: a frame booked before unmount updates nothing after it', async () => {
  const view = await mountStreaming();
  await delta('visible ');
  // Book a frame and never run it.
  await act(async () => { enqueue!(token('pending')); await Promise.resolve(); });
  expect(frames.filter(Boolean).length).toBeGreaterThan(0);

  const errors: unknown[] = [];
  const spy = vi.spyOn(console, 'error').mockImplementation((...a) => errors.push(a));
  await act(async () => { view.unmount(); });
  await act(async () => { paint(); await Promise.resolve(); });

  // React warns on a post-unmount state update; there must be nothing to warn about.
  expect(errors).toEqual([]);
  spy.mockRestore();
});

test('TEST 6: a frame booked by chat A cannot render into chat B (M-10)', async () => {
  const { fireEvent, screen } = await import('@testing-library/react');
  await mountStreaming();
  await delta('Answer for the FIRST chat. ');
  expect(document.body.textContent).toContain('Answer for the FIRST chat.');

  // A delta lands and books a frame that has NOT run yet.
  await act(async () => {
    enqueue!(token('LEAKED TEXT'));
    await Promise.resolve();
  });
  expect(frames.filter(Boolean).length).toBeGreaterThan(0);

  // The user switches to another conversation before that frame runs.
  await act(async () => {
    // The sidebar renders a mobile and a desktop copy; either is the same click.
    fireEvent.click(screen.getAllByRole('button', { name: 'Other chat' })[0]);
  });
  await waitFor(() =>
    expect(document.body.textContent).toContain('Unrelated question'),
  );

  // Chat A's callback fires here, into a view showing chat B.
  await act(async () => {
    paint();
    await Promise.resolve();
  });

  expect(document.body.textContent).not.toContain('LEAKED TEXT');
  expect(document.body.textContent).not.toContain('Answer for the FIRST chat.');
  expect(document.body.textContent).toContain('Unrelated question');

  // And chat A's own answer is intact — it kept generating in the background.
  const { getLiveStream } = await import('@/lib/streams');
  expect(getLiveStream('conv-1')?.messages.at(-1)?.content).toBe(
    'Answer for the FIRST chat. LEAKED TEXT',
  );

  await act(async () => {
    enqueue!('event: done\ndata: {}\n\n');
    finish!();
    await Promise.resolve();
  });
});

test('NEW-24: a streaming answer re-parses only its tail, not the whole answer', async () => {
  await mountStreaming();

  // A realistic markdown answer: headings, prose, lists — the shape that made
  // remark-parse superlinear.
  const chunks: string[] = [];
  for (let i = 0; i < 40; i += 1) {
    chunks.push(`## Section ${i}\n\n`);
    chunks.push(`Some prose for section ${i} with **bold** and a [link](https://example.com/${i}).\n\n`);
    chunks.push(`- first item\n- second item\n\n`);
  }

  counters.markdownParses = 0;
  counters.markdownChars = 0;
  let delivered = 0;
  for (const piece of chunks) {
    delivered += piece.length;
    // eslint-disable-next-line no-await-in-loop
    await delta(piece);
  }
  await act(async () => {
    enqueue!('event: done\ndata: {}\n\n');
    finish!();
    await Promise.resolve();
  });

  const answer = document.querySelectorAll('[data-chat-message-role="assistant"]');
  const rendered = answer[answer.length - 1] as HTMLElement;

  // The answer is fully and correctly rendered as markdown.
  expect(rendered.querySelectorAll('h2')).toHaveLength(40);
  expect(rendered.querySelectorAll('li')).toHaveLength(80);
  expect(rendered.querySelectorAll('strong')).toHaveLength(40);
  expect(rendered.textContent).not.toContain('**');

  // Had every frame re-parsed the whole answer, the characters fed to the
  // parser would be about (frames x final length) / 2 — tens of times the
  // answer itself. Segmentation keeps the total near-linear instead.
  const wholeAnswerPerFrame = (chunks.length * delivered) / 2;
  expect(counters.markdownChars).toBeLessThan(wholeAnswerPerFrame / 8);

  // eslint-disable-next-line no-console
  console.log(
    `NEW-24 parse volume: ${counters.markdownChars} chars over ${counters.markdownParses} parses ` +
      `for a ${delivered}-char answer (whole-answer-per-frame would be ~${Math.round(wholeAnswerPerFrame)})`,
  );
});

test('NEW-24: a settled block is never re-parsed once it is frozen', async () => {
  await mountStreaming();
  await delta('# Title\n\nFirst paragraph.\n\n');
  await delta('Second paragraph.\n\n');
  counters.markdownParses = 0;
  counters.markdownChars = 0;

  // Twenty more updates to a LATER block.
  for (let i = 0; i < 20; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await delta(`word${i} `);
  }

  // Each frame parses the growing tail alone. The frozen blocks above it are
  // memoized on their text, so they cost nothing at all.
  const tailOnly = 'word0 word1 word2 word3 word4 word5 word6 word7 word8 word9 '
    + 'word10 word11 word12 word13 word14 word15 word16 word17 word18 word19 ';
  expect(counters.markdownChars).toBeLessThan(tailOnly.length * 20);
  expect(document.body.textContent).toContain('First paragraph.');
  expect(document.body.textContent).toContain('word19');
});

test('DOM size of a long conversation (recorded, not asserted as a budget)', async () => {
  await mountStreaming();
  await delta('An answer with **markdown** and a list:\n\n- one\n- two\n\n');
  const scrollerEl = document.querySelector('div.relative.overflow-y-auto') as HTMLElement;
  const nodes = scrollerEl.querySelectorAll('*').length;
  const rows = scrollerEl.querySelectorAll('[data-chat-message-role]').length;
  // eslint-disable-next-line no-console
  console.log(`DOM: ${rows} message rows, ${nodes} elements in the scroller (~${Math.round(nodes / rows)} per row)`);
  expect(rows).toBe(HISTORY + 2);
});
