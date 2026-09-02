// @vitest-environment jsdom
/**
 * MULTI-DOC (2026-09-02) — documents stack to five and travel as references.
 *
 * Attaching a second document used to REPLACE the first ("a PDF stands
 * alone") — found by the owner within a minute of the 512 MB release. Now
 * up to five stack like images always did; one small document still rides
 * inline byte-for-byte, while several upload first (purpose=document) and
 * the chat request carries pdf_uploads references. Harness cloned from
 * attachment-drop, which explains makeDataTransfer/dropOn below.
 *
 * The first NEW-10 fix gated every drag handler on one question:
 *
 *     dataTransfer.types.includes('Files')
 *
 * and when the answer was no it returned WITHOUT calling preventDefault. That
 * is the whole bug. Plenty of real drag sources — VS Code, some Linux file
 * managers, some cross-window drags — advertise a file as `text/uri-list` and
 * `text/plain` and never put the word `Files` in `types`. For those our handler
 * stood aside, the event reached the `<textarea>`, and the browser did what a
 * textarea does with dropped text: it typed it in. The user got
 *
 *     file:///home/user/report.pdf
 *
 * pasted into their prompt instead of an attachment.
 *
 * WHY THE OLD TESTS PASSED ANYWAY, which matters more than the bug:
 *
 *   1. Every fixture in the old suite handed over the ideal shape,
 *      `types: ['Files']`, so the narrow gate was never actually exercised.
 *   2. jsdom implements no drag-and-drop DEFAULT ACTIONS. Failing to prevent a
 *      drop on a textarea inserts nothing in jsdom, so the symptom was invisible.
 *
 * Both are fixed here. `makeDataTransfer` reproduces the several shapes real
 * browsers produce and never normalises them, and `dropOn` applies the default
 * action jsdom omits: a drop carrying text, left un-prevented over a textarea,
 * gets typed in exactly as Chrome would. A handler that forgets preventDefault
 * now fails with the URI visibly sitting in the composer.
 */

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '@/lib/types';

/* ----------------------------------------------------------------- mocks */

type StreamCall = { turns: ChatMessage[]; context?: ChatMessage[] };
const startStream = vi.fn(async (opts: StreamCall) => {
  void opts;
  return undefined;
});
const saveMessages = vi.fn();

vi.mock('@/lib/streams', () => ({
  startStream: (opts: StreamCall) => startStream(opts),
  subscribeStreams: () => () => undefined,
  isStreaming: () => false,
  getLiveStream: () => null,
  streamingIds: () => [],
  fetchServerActive: async () => [],
  attachStream: async () => false,
  stopStream: () => undefined,
  messagesDiscardedByRegenerate: () => 0,
  clarificationAlreadySubmitted: () => false,
  markClarificationSubmitted: () => undefined,
}));

let stored: ChatMessage[] = [];

vi.mock('@/lib/history', () => ({
  newId: () => `m${Math.random().toString(36).slice(2, 10)}`,
  setEvictListener: () => undefined,
  rebuildHistoryStore: async () => {
    throw new Error('unexpected account switch in test');
  },
  getHistoryStore: () => ({
    ready: async () => undefined,
    list: () => [],
    listArchived: () => [],
    get: () => null,
    create: (title: string) => ({
      id: 'conv-1',
      title,
      messages: [],
      createdAt: 0,
      updatedAt: 0,
    }),
    saveMessages: (id: string, msgs: ChatMessage[]) => {
      stored = msgs;
      saveMessages(id, msgs);
    },
    load: async () => null,
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
  fetchMe: async () => ({ ok: true, username: 'tester', user: null }),
  userScopeKey: (me: { user: { id: number } | null; username: string }) =>
    me.user ? `u${me.user.id}` : me.username,
}));
vi.mock('@/lib/salesforceApi', () => ({
  fetchSalesforceContext: async () => ({ options: [], pending: null }),
  cancelClarification: async () => undefined,
  shouldShowStarter: () => false,
}));
vi.mock('@/lib/compact', () => ({
  isCompacting: () => false,
  requestCompact: async () => null,
}));

const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');
const {
  attachmentFile,
  clearAttachments,
  dragHasFiles,
  dropIntent,
  filesFromDrop,
} = await import('@/lib/attachments');

/* ------------------------------------------------------------- utilities */

function stubBrowserApis() {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => undefined);
  // The streaming Loader mounts a <video>; jsdom implements neither call.
  HTMLMediaElement.prototype.play = async () => undefined;
  HTMLMediaElement.prototype.pause = () => undefined;
}

/** The app as app/layout.tsx mounts it — every refusal here is a toast. */
function renderApp() {
  return render(
    <Providers>
      <ChatApp />
    </Providers>,
  );
}

const png = (name = 'shot.png', bytes = 'x') =>
  new File([bytes], name, { type: 'image/png' });

/* ------------------------------------------- realistic DataTransfer shapes */

type ItemSpec =
  | { kind: 'file'; type?: string; file: File | null; directory?: boolean }
  | { kind: 'string'; type: string; data: string };

interface DTShape {
  /** EXACTLY what the source advertises. Never defaulted to ['Files']. */
  types?: string[];
  files?: File[];
  items?: ItemSpec[];
  data?: Record<string, string>;
  /** Some sources expose no `items` collection at all. */
  noItems?: boolean;
}

/**
 * A DataTransfer stand-in reproducing one real source's shape verbatim.
 *
 * Deliberately NOT normalised: the point of this suite is that production code
 * must cope with sources that disagree about which of `types`, `items` and
 * `files` they populate. jsdom implements no DataTransfer at all, and Testing
 * Library passes a plain object straight through by reference.
 */
function makeDataTransfer(shape: DTShape): DataTransfer {
  const data: Record<string, string> = { ...(shape.data ?? {}) };
  for (const it of shape.items ?? []) {
    if (it.kind === 'string') data[it.type] = it.data;
  }
  const items = (shape.items ?? []).map((it) =>
    it.kind === 'file'
      ? {
          kind: 'file' as const,
          type: it.type ?? it.file?.type ?? '',
          getAsFile: (): File | null => it.file,
          webkitGetAsEntry: (): { isFile: boolean; isDirectory: boolean } => ({
            isFile: !it.directory,
            isDirectory: Boolean(it.directory),
          }),
        }
      : {
          kind: 'string' as const,
          type: it.type,
          getAsFile: (): File | null => null,
          webkitGetAsEntry: (): null => null,
        },
  );
  return {
    types: shape.types ?? [],
    files: shape.files ?? [],
    items: shape.noItems ? undefined : items,
    dropEffect: 'none',
    effectAllowed: 'all',
    getData: (type: string) => data[type] ?? '',
  } as unknown as DataTransfer;
}

/* --------------------------------------------------- the browser's default */

/**
 * Dispatch a drop AND apply the default action jsdom leaves out.
 *
 * This is the load-bearing part of the whole file. In Chrome a drop carrying
 * `text/uri-list` or `text/plain` over a `<textarea>` inserts that text unless
 * the event is default-prevented. jsdom does nothing at all, which is exactly
 * why the previous suite passed while the app pasted `file:///…` into people's
 * prompts. Emulating it makes "the composer must stay unchanged" a real
 * assertion rather than a tautology.
 */
function dropOn(target: Element, dt: DataTransfer): Event {
  const ev = new Event('drop', { bubbles: true, cancelable: true });
  Object.defineProperty(ev, 'dataTransfer', { value: dt });
  act(() => {
    target.dispatchEvent(ev);
  });
  if (!ev.defaultPrevented && target instanceof HTMLTextAreaElement) {
    const text = dt.getData('text/uri-list') || dt.getData('text/plain');
    if (text) {
      fireEvent.change(target, { target: { value: target.value + text } });
    }
  }
  return ev;
}

function dispatchDrag(name: string, target: Element, dt: DataTransfer): Event {
  const ev = new Event(name, { bubbles: true, cancelable: true });
  Object.defineProperty(ev, 'dataTransfer', { value: dt });
  act(() => {
    target.dispatchEvent(ev);
  });
  return ev;
}

const dragOverOn = (t: Element, dt: DataTransfer) => dispatchDrag('dragover', t, dt);
const dragEnterOn = (t: Element, dt: DataTransfer) => dispatchDrag('dragenter', t, dt);
const dragLeaveOn = (t: Element, dt: DataTransfer) => dispatchDrag('dragleave', t, dt);

/* ------------------------------------------------------------- accessors */

const dropZone = () =>
  document.querySelector('[data-file-drop-zone]') as HTMLElement;
const fileInput = () =>
  document.querySelector('input[type="file"]') as HTMLInputElement;
const textarea = () =>
  screen.getByRole('textbox', { name: 'Message' }) as HTMLTextAreaElement;
const chips = () =>
  Array.from(
    document.querySelectorAll('button[aria-label^="Remove attachment"]'),
  );
const overlay = () => screen.queryByText('Drop files to attach');

beforeEach(() => {
  stubBrowserApis();
  stored = [];
  startStream.mockClear();
  saveMessages.mockClear();
  clearAttachments();
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(async () => {
  // The composer reads every accepted file with a fire-and-forget FileReader.
  // Unmounting while one is still in flight lets it resolve into a torn-down
  // jsdom, which surfaces as an unhandled "window is not defined" from React's
  // scheduler. Letting the queue drain first keeps the run clean.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  cleanup();
  vi.unstubAllGlobals();
  clearAttachments();
});

/* ======================== CASE A — the ideal OS drag (must keep working) */



const pdf = (name: string, bytes = '%PDF-1.4 tiny') =>
  new File([bytes], name, { type: 'application/pdf' });

const dropFiles = (...files: File[]) =>
  dropOn(
    dropZone(),
    makeDataTransfer({
      types: ['Files'],
      files,
      items: files.map((file) => ({ kind: 'file', file })),
    }),
  );

describe('documents stack', () => {
  it('a second document JOINS the first instead of replacing it', async () => {
    renderApp();
    dropFiles(pdf('a.pdf'), pdf('b.pdf'));
    await waitFor(() => expect(chips().length).toBe(2));
    const labels = chips().map((c) => c.getAttribute('aria-label') ?? '');
    expect(labels.join(' ')).toContain('a.pdf');
    expect(labels.join(' ')).toContain('b.pdf');
  });

  it('a burst of six caps silently at five; the NEXT attempt gets the message', async () => {
    renderApp();
    // One drop of six: FileReader resolves them concurrently, so the cap is
    // applied inside the state update (silently, like images always have).
    dropFiles(...[1, 2, 3, 4, 5, 6].map((i) => pdf(`d${i}.pdf`)));
    await waitFor(() => expect(chips().length).toBe(5));
    // A deliberate seventh, attached once the five are settled, is told why.
    dropFiles(pdf('d7.pdf'));
    expect(await screen.findByText(/up to 5 documents/i)).toBeTruthy();
    expect(chips().length).toBe(5);
  });
});

describe('sending several documents', () => {
  it('uploads each with purpose=document and streams references', async () => {
    const uploads: FormData[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        if (String(url) === '/api/upload') {
          uploads.push(init?.body as FormData);
          return {
            ok: true,
            status: 200,
            json: async () => ({
              upload_id: `${uploads.length}`.repeat(32).slice(0, 32),
              filename: (init?.body as FormData).get('file') instanceof File
                ? ((init?.body as FormData).get('file') as File).name
                : 'doc.pdf',
            }),
          };
        }
        return { ok: true, status: 200, json: async () => ({}) };
      }),
    );
    renderApp();
    dropFiles(pdf('one.pdf'), pdf('two.pdf'));
    await waitFor(() => expect(chips().length).toBe(2));

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'compare them' },
    });
    fireEvent.click(screen.getByRole('button', { name: /send message/i }));

    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    // Both documents streamed BEFORE the chat request…
    expect(uploads.length).toBe(2);
    for (const form of uploads) {
      expect(form.get('purpose')).toBe('document');
    }
    // …and the request carries references, never inline base64.
    const opts = startStream.mock.calls[0][0] as Record<string, unknown>;
    expect((opts.pdfUploads as unknown[]).length).toBe(2);
    expect(opts.pdf ?? null).toBeNull();
  });

  it('ONE small document keeps the inline wire exactly as before', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    renderApp();
    dropFiles(pdf('solo.pdf'));
    await waitFor(() => expect(chips().length).toBe(1));

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'summarise' },
    });
    fireEvent.click(screen.getByRole('button', { name: /send message/i }));

    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    const opts = startStream.mock.calls[0][0] as Record<string, unknown>;
    expect(typeof opts.pdf).toBe('string');
    expect((opts.pdf as string).length).toBeGreaterThan(0);
    expect(opts.pdfUploads ?? null).toBeNull();
    // Background chrome (auth pings) may fetch; the UPLOAD rail must not.
    const uploadCalls = fetchMock.mock.calls.filter(([u]: [unknown]) =>
      String(u).startsWith('/api/upload'),
    );
    expect(uploadCalls.length).toBe(0);
  });
});
