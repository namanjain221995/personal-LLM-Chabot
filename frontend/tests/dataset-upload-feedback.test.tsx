// @vitest-environment jsdom
/**
 * H-01 — a dataset send must be visible while it is uploading.
 *
 * A dataset does not ride inside the chat request. It streams to /api/upload
 * first, and only then does a generation open. The thread, however, was drawn
 * exclusively by the stream manager: `persist()` writes the store and the
 * sidebar but never React state, so the user's turn appeared as a side effect
 * of `register()` inside `startStream`.
 *
 * For a dataset that is the wrong order. The composer cleared instantly, the
 * upload ran for as long as a 200 MB file takes, and in that window the thread
 * showed nothing new at all — on a first message, the empty state. If the
 * upload then failed, the turn never appeared, so the prompt looked lost.
 *
 * These tests drive the REAL ChatApp with only the upload and the stream
 * mocked, because the bug was never in a helper — it was in which of them owns
 * putting a message on screen. The load-bearing assertions are the ones taken
 * WHILE the upload promise is still pending, and the H-02 invariant that a
 * failed upload starts no generation whatsoever.
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
const startStream = vi.fn(async (_opts: StreamCall) => undefined);
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
    setActiveUser: () => undefined,
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
  fetchMe: async () => ({ ok: true, username: 'tester' }),
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

/* ------------------------------------------------------------- utilities */

/** A pending fetch whose outcome the test decides, mid-assertion. */
function deferredUpload() {
  let settle!: (res: unknown) => void;
  let fail!: (err: unknown) => void;
  const promise = new Promise((resolve, reject) => {
    settle = resolve;
    fail = reject;
  });
  const fetchMock = vi.fn(() => promise);
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, settle, fail };
}

const jsonResponse = (body: unknown, ok = true, status = 200) => ({
  ok,
  status,
  json: async () => body,
});

/** Attach `name` as a file, type `text`, press Send. */
function sendWith(name: string, text: string, type = 'text/csv') {
  const input = document.querySelector(
    'input[type="file"]',
  ) as HTMLInputElement;
  const file = new File(['col_a,col_b\n1,2\n'], name, { type });
  fireEvent.change(input, { target: { files: [file] } });

  const box = screen.getByRole('textbox', { name: 'Message' });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
}

/** Browser APIs ChatApp touches on mount that jsdom does not implement. */
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
  HTMLMediaElement.prototype.play =
    HTMLMediaElement.prototype.play ?? (async () => undefined);
}

beforeEach(() => {
  stubBrowserApis();
  stored = [];
  startStream.mockClear();
  saveMessages.mockClear();
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/* --------------------------------------------- while the upload is pending */

describe('while /api/upload is still unresolved', () => {
  it('shows the user turn, the filename and an honest pending state', async () => {
    deferredUpload();
    render(<ChatApp />);

    sendWith('sales.csv', 'What is the highest amount?');

    // Nothing has been awaited yet by the assertions below — the upload
    // promise is deliberately still pending for all three.
    expect(
      await screen.findByText('What is the highest amount?'),
    ).toBeTruthy(); // H01-01
    expect(screen.getByText('sales.csv')).toBeTruthy(); // H01-02
    expect(screen.getByText('Uploading dataset…')).toBeTruthy(); // H01-03
  });

  it('has NOT started a generation yet', async () => {
    deferredUpload();
    render(<ChatApp />);

    sendWith('sales.csv', 'analyse this');
    await screen.findByText('analyse this');

    // H01-04: the turn is on screen and no stream exists — the two are
    // genuinely independent now.
    expect(startStream).not.toHaveBeenCalled();
  });

  it('offers no Stop button, because nothing is generating', async () => {
    deferredUpload();
    render(<ChatApp />);

    sendWith('sales.csv', 'analyse this');
    await screen.findByText('analyse this');

    // H01-05: Stop belongs to a running generation. During an upload it was
    // both untrue and inert — there is no registered stream to abort.
    expect(
      screen.queryByRole('button', { name: 'Stop generating' }),
    ).toBeNull();
  });

  it('still persisted the turn, so a reload does not lose it', async () => {
    deferredUpload();
    render(<ChatApp />);

    sendWith('sales.csv', 'analyse this');
    await screen.findByText('analyse this');

    expect(stored.at(-1)).toMatchObject({
      role: 'user',
      content: 'analyse this',
      pdfName: 'sales.csv',
    });
  });
});

/* ------------------------------------------------------ successful upload */

describe('when the upload succeeds', () => {
  it('clears pending, links upload_id and starts exactly one generation', async () => {
    const { settle } = deferredUpload();
    render(<ChatApp />);

    sendWith('sales.csv', 'analyse this');
    await screen.findByText('analyse this');

    await act(async () => {
      settle(jsonResponse({ upload_id: 'up-42', files: 3 }));
    });

    // H01-06
    await waitFor(() =>
      expect(screen.queryByText('Uploading dataset…')).toBeNull(),
    );
    // H01-08 — exactly once, not zero and not twice.
    expect(startStream).toHaveBeenCalledTimes(1);
    // H01-07 — H-07's linkage survives: the turn names the row the server made.
    const sent = startStream.mock.calls[0][0];
    expect(sent.turns.at(-1)?.meta?.attachments?.[0]).toMatchObject({
      name: 'sales.csv',
      kind: 'dataset',
      id: 'up-42',
    });
  });

  it('keeps the turn and its file on screen once generation begins', async () => {
    const { settle } = deferredUpload();
    render(<ChatApp />);

    sendWith('sales.csv', 'analyse this');
    await screen.findByText('analyse this');
    await act(async () => {
      settle(jsonResponse({ upload_id: 'up-1', files: 1 }));
    });

    expect(screen.getByText('analyse this')).toBeTruthy();
    expect(screen.getByText('sales.csv')).toBeTruthy();
  });
});

/* ---------------------------------------------------------- failed upload */

/** The three ways an upload can fail, all of which must behave identically. */
const failures: Array<[string, () => { settle: (r: unknown) => void }]> = [
  [
    'a non-2xx response',
    () => {
      const d = deferredUpload();
      return {
        settle: () =>
          d.settle(jsonResponse({ detail: 'That file is too large.' }, false, 413)),
      };
    },
  ],
  [
    'a malformed, non-JSON body',
    () => {
      const d = deferredUpload();
      return {
        settle: () =>
          d.settle({
            ok: true,
            status: 200,
            json: async () => {
              throw new SyntaxError('Unexpected token < in JSON');
            },
          }),
      };
    },
  ],
  [
    'a rejected fetch (network down)',
    () => {
      const d = deferredUpload();
      return { settle: () => d.fail(new TypeError('Failed to fetch')) };
    },
  ],
];

describe.each(failures)('when the upload fails with %s', (_label, make) => {
  it('keeps the turn and the prompt, marks it failed, and generates NOTHING', async () => {
    const { settle } = make();
    render(<ChatApp />);

    sendWith('sales.csv', 'What is the highest amount?');
    await screen.findByText('What is the highest amount?');

    await act(async () => {
      settle(undefined);
      await Promise.resolve();
    });

    // H01-09 / H01-10 — the user's words do not vanish because a request did.
    await waitFor(() =>
      expect(screen.getByText('What is the highest amount?')).toBeTruthy(),
    );
    // H01-11 — the pending state is gone…
    expect(screen.queryByText('Uploading dataset…')).toBeNull();
    // …replaced by something truthful and in-thread, not only a toast.
    expect(
      screen.getByText(/Dataset upload failed/i),
    ).toBeTruthy();
    // H01-02 on the failure path: the file is still understandable.
    expect(screen.getByText('sales.csv')).toBeTruthy();

    // H01-12 / H01-14 — THE H-02 INVARIANT. A dataset that never arrived must
    // not be answered from, so no generation may begin. Zero. Not "later".
    expect(startStream).not.toHaveBeenCalled();
    // H01-13 — and no assistant placeholder was invented to hold the error.
    expect(stored.some((m) => m.role === 'assistant')).toBe(false);
  });
});

describe('H-02 regression, stated on its own', () => {
  it('never calls startStream on an upload failure, however long we wait', async () => {
    const { fail } = deferredUpload();
    render(<ChatApp />);

    sendWith('data.xlsx', 'summarise');
    await screen.findByText('summarise');

    await act(async () => {
      fail(new Error('upload exploded'));
      await Promise.resolve();
    });
    // Let every microtask and the retry-free failure path drain.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(startStream).toHaveBeenCalledTimes(0);
  });
});

/* ------------------------------------------- non-dataset sends are untouched */

describe('sends that are not datasets behave exactly as before', () => {
  it('a text-only send streams immediately and shows no upload state', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    render(<ChatApp />);

    const box = screen.getByRole('textbox', { name: 'Message' });
    fireEvent.change(box, { target: { value: 'just a question' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));

    // H01-16: no upload is involved, so generation starts at once.
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('Uploading dataset…')).toBeNull();
    expect(fetchMock).not.toHaveBeenCalledWith(
      '/api/upload',
      expect.anything(),
    );
  });

  it('an image send streams immediately and shows no upload state', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    render(<ChatApp />);

    // H01-17: an image travels as base64 INSIDE the chat request, so it must
    // never be routed through /api/upload.
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const png = new File(['\u0089PNG'], 'chart.png', { type: 'image/png' });
    fireEvent.change(input, { target: { files: [png] } });
    // The image preview is read asynchronously by FileReader.
    await waitFor(() =>
      expect(document.querySelector('img[alt="Attached: chart.png"]')).toBeTruthy(),
    );

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'what does this show?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('Uploading dataset…')).toBeNull();
    expect(fetchMock).not.toHaveBeenCalledWith(
      '/api/upload',
      expect.anything(),
    );
  });

  it('a PDF send streams immediately and shows no upload state', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    render(<ChatApp />);

    // H01-18: a PDF travels as base64 INSIDE the chat request — it must never
    // be routed through the dataset upload path.
    const input = document.querySelector(
      'input[type="file"]',
    ) as HTMLInputElement;
    const pdf = new File(['%PDF-1.4'], 'report.pdf', {
      type: 'application/pdf',
    });
    fireEvent.change(input, { target: { files: [pdf] } });
    await screen.findByText('report.pdf');

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'what is in this?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));

    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('Uploading dataset…')).toBeNull();
  });
});
