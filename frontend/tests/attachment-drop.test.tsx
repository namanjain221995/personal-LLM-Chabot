// @vitest-environment jsdom
/**
 * NEW-10A — a file drag must deliver the FILE, never a link to it.
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

describe('CASE A — standard OS file drag', () => {
  it('attaches the file', async () => {
    // NEW10A-01
    renderApp();

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: ['Files'],
        files: [png()],
        items: [{ kind: 'file', file: png() }],
      }),
    );

    await waitFor(() => expect(chips().length).toBe(1));
    expect(screen.getByText('shot.png')).toBeTruthy();
  });

  it('shows the drop indication and prevents navigation', () => {
    // NEW10A-02
    const dt = makeDataTransfer({
      types: ['Files'],
      items: [{ kind: 'file', file: png() }],
    });
    renderApp();

    dragEnterOn(dropZone(), dt);
    expect(overlay()).not.toBe(null);

    const over = dragOverOn(dropZone(), dt);
    expect(over.defaultPrevented).toBe(true);
    expect(dt.dropEffect).toBe('copy');
  });
});

/* =============== CASE B — file items, but no "Files" in types (the bug) */

describe('CASE B — file items without a Files type', () => {
  it('still attaches, because an item of kind "file" IS a file', async () => {
    // NEW10A-03 — the exact shape the old types.includes("Files") gate missed.
    renderApp();

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: [],
        files: [],
        items: [{ kind: 'file', type: 'image/png', file: png('from-item.png') }],
      }),
    );

    await waitFor(() => expect(chips().length).toBe(1));
    expect(screen.getByText('from-item.png')).toBeTruthy();
  });

  it('activates the drop indication on dragenter too', () => {
    // NEW10A-04 — during a drag the bytes are hidden, but `kind` is not.
    renderApp();

    dragEnterOn(
      dropZone(),
      makeDataTransfer({
        types: [],
        items: [{ kind: 'file', type: 'application/pdf', file: null }],
      }),
    );

    expect(overlay()).not.toBe(null);
  });
});

/* ================================ CASE C — a File and a URI in one drag */

describe('CASE C — File and URI together', () => {
  it('prefers the File and ignores the URI', async () => {
    // NEW10A-05
    renderApp();
    fireEvent.change(textarea(), { target: { value: 'hello' } });

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: ['Files', 'text/uri-list', 'text/plain'],
        files: [new File(['%PDF'], 'report.pdf', { type: 'application/pdf' })],
        items: [
          {
            kind: 'file',
            file: new File(['%PDF'], 'report.pdf', { type: 'application/pdf' }),
          },
          {
            kind: 'string',
            type: 'text/uri-list',
            data: 'file:///home/u/report.pdf',
          },
          {
            kind: 'string',
            type: 'text/plain',
            data: 'file:///home/u/report.pdf',
          },
        ],
      }),
    );

    await waitFor(() => expect(screen.getByText('report.pdf')).toBeTruthy());
    expect(chips().length).toBe(1);
    expect(textarea().value).toBe('hello');
  });

  it('adds the file exactly once, not once per representation', async () => {
    // NEW10A-06 — `items` and `files` describe the SAME file, and real browsers
    // do not guarantee reference identity between the two.
    renderApp();

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: ['Files', 'text/uri-list'],
        files: [png('once.png')],
        items: [
          { kind: 'file', file: png('once.png') },
          { kind: 'string', type: 'text/uri-list', data: 'file:///tmp/once.png' },
        ],
      }),
    );

    await waitFor(() => expect(chips().length).toBe(1));
    await act(async () => {
      await Promise.resolve();
    });
    expect(chips().length).toBe(1);
  });
});

/* ============================= CASE D — only the files collection is set */

describe('CASE D — files collection only', () => {
  it('attaches from dataTransfer.files when items is absent', async () => {
    // NEW10A-07
    renderApp();

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: [],
        files: [png('only-files.png')],
        noItems: true,
      }),
    );

    await waitFor(() => expect(chips().length).toBe(1));
    expect(screen.getByText('only-files.png')).toBeTruthy();
  });

  it('attaches when items is present but empty', async () => {
    // NEW10A-08
    renderApp();

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: [],
        files: [png('empty-items.png')],
        items: [],
      }),
    );

    await waitFor(() => expect(chips().length).toBe(1));
  });
});

/* ================================================= CASE E — URI only */

describe('CASE E — a file URI with no bytes behind it', () => {
  it('never pastes the URI into the composer', async () => {
    // NEW10A-09 — THE manual bug. The harness types the URI in if we fail to
    // prevent the default, so this assertion has teeth.
    renderApp();
    fireEvent.change(textarea(), { target: { value: 'hello' } });

    const ev = dropOn(
      textarea(),
      makeDataTransfer({
        types: ['text/uri-list', 'text/plain'],
        items: [
          {
            kind: 'string',
            type: 'text/uri-list',
            data: 'file:///home/user/report.pdf',
          },
          {
            kind: 'string',
            type: 'text/plain',
            data: 'file:///home/user/report.pdf',
          },
        ],
      }),
    );

    expect(ev.defaultPrevented).toBe(true);
    expect(textarea().value).toBe('hello');
    expect(chips().length).toBe(0);
  });

  it('explains that the source handed over a link, not the file', async () => {
    // NEW10A-10
    renderApp();

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: ['text/uri-list', 'text/plain'],
        items: [
          {
            kind: 'string',
            type: 'text/uri-list',
            data: 'file:///home/user/report.pdf',
          },
        ],
      }),
    );

    expect(
      await screen.findByText(/file link, not the file itself/i),
    ).toBeTruthy();
  });
});

/* ========================================== CASE F — VS Code style URIs */

describe('CASE F — editor and local URIs', () => {
  it.each([
    'vscode-file://vscode-app/home/user/report.pdf',
    'vscode-remote://ssh-remote%2Bbox/home/user/notes.md',
    'content://com.android.providers/document/1234',
    '/home/user/plain/absolute/path.pdf',
    'C:\\Users\\me\\report.pdf',
  ])('refuses to paste %s', (uri) => {
    // NEW10A-11 … 15
    renderApp();
    fireEvent.change(textarea(), { target: { value: 'hello' } });

    const ev = dropOn(
      textarea(),
      makeDataTransfer({
        types: ['text/uri-list', 'text/plain'],
        items: [{ kind: 'string', type: 'text/plain', data: uri }],
      }),
    );

    expect(ev.defaultPrevented).toBe(true);
    expect(textarea().value).toBe('hello');
    expect(chips().length).toBe(0);
  });

  it('suppresses even an http URI when the drag CLAIMED to carry files', () => {
    // NEW10A-16 — a file drag whose bytes never arrived must not degrade into
    // pasting whatever link the source also happened to attach.
    renderApp();
    fireEvent.change(textarea(), { target: { value: 'hello' } });

    const ev = dropOn(
      textarea(),
      makeDataTransfer({
        types: ['Files', 'text/uri-list'],
        items: [
          { kind: 'file', file: null }, // advertised, yielded nothing
          {
            kind: 'string',
            type: 'text/uri-list',
            data: 'http://host/report.pdf',
          },
        ],
      }),
    );

    expect(ev.defaultPrevented).toBe(true);
    expect(textarea().value).toBe('hello');
  });
});

/* ================================= CASE G — ordinary links must still work */

describe('CASE G — an ordinary web link', () => {
  it('is left entirely to the browser', () => {
    // NEW10A-17 — no file evidence and no local scheme: not ours to touch.
    renderApp();
    fireEvent.change(textarea(), { target: { value: 'hello ' } });

    const ev = dropOn(
      textarea(),
      makeDataTransfer({
        types: ['text/uri-list', 'text/plain'],
        items: [
          { kind: 'string', type: 'text/uri-list', data: 'https://openai.com' },
          { kind: 'string', type: 'text/plain', data: 'https://openai.com' },
        ],
      }),
    );

    expect(ev.defaultPrevented).toBe(false);
    // The harness then applied the browser default, which is correct here.
    expect(textarea().value).toBe('hello https://openai.com');
    expect(chips().length).toBe(0);
  });

  it('does not prevent dragover for a plain text drag', () => {
    // NEW10A-18
    renderApp();

    const ev = dragOverOn(
      dropZone(),
      makeDataTransfer({
        types: ['text/plain'],
        items: [{ kind: 'string', type: 'text/plain', data: 'some words' }],
      }),
    );

    expect(ev.defaultPrevented).toBe(false);
    expect(overlay()).toBe(null);
  });

  it('shows no drop indication for a link drag', () => {
    // NEW10A-19
    renderApp();

    dragEnterOn(
      dropZone(),
      makeDataTransfer({
        types: ['text/uri-list'],
        items: [
          { kind: 'string', type: 'text/uri-list', data: 'https://example.com' },
        ],
      }),
    );

    expect(overlay()).toBe(null);
  });
});

/* ============================== CASE H — dropping straight on the textarea */

describe('CASE H — a real file dropped on the textarea itself', () => {
  it('attaches the file and leaves the typed text alone', async () => {
    // NEW10A-20 — the drop region owns the event before the textarea default.
    renderApp();
    fireEvent.change(textarea(), { target: { value: 'hello' } });

    const ev = dropOn(
      textarea(),
      makeDataTransfer({
        types: ['Files', 'text/uri-list', 'text/plain'],
        files: [new File(['%PDF'], 'report.pdf', { type: 'application/pdf' })],
        items: [
          {
            kind: 'file',
            file: new File(['%PDF'], 'report.pdf', { type: 'application/pdf' }),
          },
          {
            kind: 'string',
            type: 'text/plain',
            data: 'file:///home/u/report.pdf',
          },
        ],
      }),
    );

    expect(ev.defaultPrevented).toBe(true);
    await waitFor(() => expect(screen.getByText('report.pdf')).toBeTruthy());
    expect(textarea().value).toBe('hello');
  });
});

/* ================================================= CASE I — several files */

describe('CASE H2 — an inner handler that swallows the event', () => {
  it('still attaches, because the region listens in the CAPTURE phase', async () => {
    // NEW10A-20b — bubble-phase binding would never see this drop: the inner
    // listener halts propagation before it can climb back out to the region.
    // Capture means the region has already handled it on the way down.
    renderApp();
    const ta = textarea();
    ta.addEventListener('drop', (e) => e.stopPropagation());
    fireEvent.change(ta, { target: { value: 'hello' } });

    const ev = dropOn(
      ta,
      makeDataTransfer({
        types: ['Files', 'text/plain'],
        files: [png('captured.png')],
        items: [
          { kind: 'file', file: png('captured.png') },
          { kind: 'string', type: 'text/plain', data: 'file:///tmp/captured.png' },
        ],
      }),
    );

    expect(ev.defaultPrevented).toBe(true);
    await waitFor(() => expect(screen.getByText('captured.png')).toBeTruthy());
    expect(textarea().value).toBe('hello');
  });
});

describe('CASE I — multiple file items', () => {
  it('runs every file through the shared pipeline and keeps the limits', async () => {
    // NEW10A-21
    const six = Array.from({ length: 6 }, (_, i) => png(`p${i}.png`));
    renderApp();

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: ['Files'],
        files: six,
        items: six.map((f) => ({ kind: 'file' as const, file: f })),
      }),
    );

    await waitFor(() => expect(chips().length).toBe(5));
    expect(
      screen.getByText(/You can attach up to 5 images — 1 file was left out\./),
    ).toBeTruthy();
  });

  it('accepts a formerly-unsupported dropped file onto the document rail', async () => {
    // NEW10A-22, inverted 2026-09-02: every file type is accepted now
    // ("upload anything") — a video the engine cannot watch still gets
    // attached, streamed, and named honestly by the server instead of being
    // turned away at the door.
    const clip = new File(['x'], 'clip.mp4', { type: 'video/mp4' });
    renderApp();

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: ['Files'],
        files: [clip],
        items: [{ kind: 'file', file: clip }],
      }),
    );

    await waitFor(() => expect(chips().length).toBe(1));
    // The chip is the remove button; the filename lives in its aria-label.
    expect(chips()[0].getAttribute('aria-label')).toContain('clip.mp4');
  });

  it('gives an oversized dropped file the picker size message', async () => {
    // NEW10A-23
    const huge = png('huge.png');
    Object.defineProperty(huge, 'size', { value: 11 * 1024 * 1024 });
    renderApp();

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: ['Files'],
        files: [huge],
        items: [{ kind: 'file', file: huge }],
      }),
    );

    expect(
      await screen.findByText(/huge\.png is 11\.0 MB — the limit is 10 MB\./),
    ).toBeTruthy();
  });
});

/* ================================================ CASE J — a dropped folder */

describe('CASE J — a dropped directory', () => {
  it('is rejected, and its path is not pasted either', async () => {
    // NEW10A-24
    const dir = new File([], 'reports', { type: '' });
    renderApp();
    fireEvent.change(textarea(), { target: { value: 'hello' } });

    const ev = dropOn(
      textarea(),
      makeDataTransfer({
        types: ['Files', 'text/uri-list'],
        files: [dir],
        items: [
          { kind: 'file', file: dir, directory: true },
          {
            kind: 'string',
            type: 'text/uri-list',
            data: 'file:///home/user/reports',
          },
        ],
      }),
    );

    expect(ev.defaultPrevented).toBe(true);
    expect(await screen.findByText(/Folders can/)).toBeTruthy();
    expect(chips().length).toBe(0);
    expect(textarea().value).toBe('hello');
  });
});

/* =========================================== state, pipeline and handshake */

describe('the shared pipeline and application state', () => {
  it('still accepts files from the "+" menu input', async () => {
    // NEW10A-25
    renderApp();

    fireEvent.change(fileInput(), { target: { files: [png('picked.png')] } });

    await waitFor(() => expect(chips().length).toBe(1));
  });

  it('sends a dropped dataset down the unchanged /api/upload path', async () => {
    // NEW10A-26 — no dataset-specific code in the drop handler.
    const fetchMock = vi.fn(async (input: unknown) => {
      void input;
      return {
        ok: true,
        status: 200,
        json: async () => ({ files: 1, upload_id: 'up-1' }),
      };
    });
    vi.stubGlobal('fetch', fetchMock);
    renderApp();

    const csv = new File(['a,b\n1,2\n'], 'sales.csv', { type: 'text/csv' });
    dropOn(
      dropZone(),
      makeDataTransfer({
        types: ['Files'],
        files: [csv],
        items: [{ kind: 'file', file: csv }],
      }),
    );
    await waitFor(() => expect(screen.getByText('sales.csv')).toBeTruthy());

    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some((c) => String(c[0]) === '/api/upload'),
      ).toBe(true),
    );
  });

  it('refuses a drop while an answer is streaming, like the "+" menu', async () => {
    // NEW10A-27
    renderApp();
    fireEvent.change(textarea(), { target: { value: 'hello' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    await screen.findByRole('button', { name: 'Stop generating' });

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: ['Files'],
        files: [png()],
        items: [{ kind: 'file', file: png() }],
      }),
    );

    expect(
      await screen.findByText(/Wait for the answer to finish/),
    ).toBeTruthy();
    expect(chips().length).toBe(0);
  });

  it('does not flicker across nested children', () => {
    // NEW10A-28
    const dt = makeDataTransfer({
      types: ['Files'],
      items: [{ kind: 'file', file: png() }],
    });
    renderApp();
    const inner = fileInput().closest('div') as HTMLElement;

    dragEnterOn(dropZone(), dt);
    expect(overlay()).not.toBe(null);
    dragEnterOn(inner, dt);
    dragLeaveOn(dropZone(), dt);
    expect(overlay()).not.toBe(null);
    dragLeaveOn(inner, dt);
    expect(overlay()).toBe(null);
  });

  it('keeps the dropped file previewable on the message it was sent with', async () => {
    // NEW10A-29 — NEW-09A and NEW-10A must agree on attachment identity.
    renderApp();

    dropOn(
      dropZone(),
      makeDataTransfer({
        types: ['Files'],
        files: [png('drop.png')],
        items: [{ kind: 'file', file: png('drop.png') }],
      }),
    );
    await waitFor(() => expect(chips().length).toBe(1));

    fireEvent.change(textarea(), { target: { value: 'what is this?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => expect(stored.length).toBeGreaterThan(0));
    expect(attachmentFile(stored[0].id, 0)?.name).toBe('drop.png');
  });

  it('is the only drop region, and never the sidebar', () => {
    // NEW10A-30
    renderApp();
    expect(document.querySelectorAll('[data-file-drop-zone]').length).toBe(1);
    const nav = document.querySelector('nav, aside') as HTMLElement | null;
    if (nav) expect(nav.closest('[data-file-drop-zone]')).toBe(null);
  });
});

/* ====================================================== the pure helpers */

describe('drag helpers', () => {
  it('detects files from any of the three witnesses', () => {
    // NEW10A-31
    expect(dragHasFiles(makeDataTransfer({ types: ['Files'] }))).toBe(true);
    expect(
      dragHasFiles(makeDataTransfer({ items: [{ kind: 'file', file: null }] })),
    ).toBe(true);
    expect(
      dragHasFiles(makeDataTransfer({ files: [png()], noItems: true })),
    ).toBe(true);
    expect(
      dragHasFiles(
        makeDataTransfer({
          types: ['text/plain'],
          items: [{ kind: 'string', type: 'text/plain', data: 'x' }],
        }),
      ),
    ).toBe(false);
    expect(dragHasFiles(null)).toBe(false);
  });

  it('merges items and files without duplicating', () => {
    // NEW10A-32
    const out = filesFromDrop(
      makeDataTransfer({
        types: ['Files'],
        files: [png('a.png'), png('b.png')],
        items: [{ kind: 'file', file: png('a.png') }],
      }),
    );
    expect(out.files.map((f) => f.name).sort()).toEqual(['a.png', 'b.png']);
    expect(out.directories).toBe(0);
  });

  it('separates directories and never re-adds them from files', () => {
    // NEW10A-33
    const dir = new File([], 'reports', { type: '' });
    const out = filesFromDrop(
      makeDataTransfer({
        types: ['Files'],
        files: [dir],
        items: [{ kind: 'file', file: dir, directory: true }],
      }),
    );
    expect(out.files).toEqual([]);
    expect(out.directories).toBe(1);
  });

  it('classifies each drop into exactly one intent', () => {
    // NEW10A-34
    expect(
      dropIntent(
        makeDataTransfer({ types: ['Files'], files: [png()], noItems: true }),
      ).action,
    ).toBe('files');
    expect(
      dropIntent(
        makeDataTransfer({
          types: ['text/uri-list'],
          items: [
            { kind: 'string', type: 'text/uri-list', data: 'file:///tmp/a.pdf' },
          ],
        }),
      ).action,
    ).toBe('file-uri');
    expect(
      dropIntent(
        makeDataTransfer({
          types: ['text/uri-list'],
          items: [
            {
              kind: 'string',
              type: 'text/uri-list',
              data: 'https://example.com',
            },
          ],
        }),
      ).action,
    ).toBe('ignore');
    expect(
      dropIntent(
        makeDataTransfer({
          types: ['Files'],
          items: [{ kind: 'file', file: null, directory: true }],
        }),
      ).action,
    ).toBe('directories');
  });
});
