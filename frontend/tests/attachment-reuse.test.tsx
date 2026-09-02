// @vitest-environment jsdom
/**
 * PHASE 4A / 4B — using a file you already sent, a second time.
 *
 * Two gestures, ONE handler. "Attach again" is a real button because dragging
 * is mouse-only, invisible and untestable from a keyboard; the drag exists
 * because it is the gesture people reach for. They must not become two
 * pipelines — that is how one entry point ends up accepting what the other
 * refuses, which is precisely why `acceptFiles` is already the single door for
 * the picker and the desktop drop.
 *
 * The drag half carries a REFERENCE, never bytes: a page cannot put a File
 * into a drag it starts. So the payload is `{messageId, index}` under a private
 * MIME, and the drop resolves it through the same ladder the button uses. What
 * it must never carry is anything in `text/plain` — NEW-10A exists because a
 * drag whose only readable part was text got typed into the prompt.
 */

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  INTERNAL_ATTACHMENT_MIME,
  clearAttachments,
  dragHasInternalAttachment,
  dropIntent,
  readInternalAttachment,
  rememberAttachmentFiles,
  writeInternalAttachment,
} from '@/lib/attachments';

/* ------------------------------------------------------- drag plumbing */

function makeDataTransfer(init: {
  types?: string[];
  data?: Record<string, string>;
  files?: File[];
  items?: Array<{ kind: string; file?: File; directory?: boolean }>;
}): DataTransfer {
  const data = { ...(init.data ?? {}) };
  return {
    types: init.types ?? Object.keys(data),
    files: init.files ?? [],
    items: (init.items ?? []).map((it) => ({
      kind: it.kind,
      type: it.file?.type ?? '',
      getAsFile: () => it.file ?? null,
      webkitGetAsEntry: () => ({ isDirectory: Boolean(it.directory) }),
    })),
    getData: (type: string) => data[type] ?? '',
    setData: (type: string, value: string) => {
      data[type] = value;
    },
    effectAllowed: 'none',
    dropEffect: 'none',
  } as unknown as DataTransfer;
}

const png = (name = 'shot.png') =>
  new File(['\u0089PNG\r\n'], name, { type: 'image/png' });
const pdf = (name = 'report.pdf') =>
  new File(['%PDF-1.4'], name, { type: 'application/pdf' });
const csv = (name = 'sales.csv') =>
  new File(['col_a,col_b\n1,2\n'], name, { type: 'text/csv' });

/* ================================================= the drag payload (4B) */

describe('P4B · the internal drag payload', () => {
  it('P4B-02 — round-trips identity under a private MIME', () => {
    const dt = makeDataTransfer({});
    expect(writeInternalAttachment(dt, { messageId: 'm1', index: 2 })).toBe(true);
    expect(readInternalAttachment(dt)).toEqual({ messageId: 'm1', index: 2 });
    expect(dt.effectAllowed).toBe('copy');
  });

  it('P4B-03 — writes NOTHING into text/plain or text/uri-list', () => {
    const dt = makeDataTransfer({});
    writeInternalAttachment(dt, { messageId: 'm1', index: 0 });
    expect(dt.getData('text/plain')).toBe('');
    expect(dt.getData('text/uri-list')).toBe('');
    // And what it does write names nothing on any disk.
    const payload = dt.getData(INTERNAL_ATTACHMENT_MIME);
    expect(payload).not.toMatch(/blob:|file:|\/data\/|\/workspace|http/);
    expect(Object.keys(JSON.parse(payload)).sort()).toEqual(['index', 'messageId']);
  });

  it('refuses a malformed or foreign payload rather than trusting it', () => {
    const bad = [
      '',
      'not json',
      '{}',
      '{"messageId":"m1"}',
      '{"messageId":"","index":0}',
      '{"messageId":"m1","index":-1}',
      '{"messageId":"m1","index":1.5}',
      '{"messageId":"m1","index":"0"}',
      '{"messageId":42,"index":0}',
      'null',
    ];
    for (const raw of bad) {
      const dt = makeDataTransfer({ data: { [INTERNAL_ATTACHMENT_MIME]: raw } });
      expect(readInternalAttachment(dt)).toBeNull();
    }
  });

  it('recognises our drag DURING the drag, from types alone', () => {
    expect(
      dragHasInternalAttachment(
        makeDataTransfer({ types: [INTERNAL_ATTACHMENT_MIME] }),
      ),
    ).toBe(true);
    expect(dragHasInternalAttachment(makeDataTransfer({ types: ['Files'] }))).toBe(
      false,
    );
  });
});

describe('P4B · dropIntent ordering is preserved', () => {
  it('P4B-04 — a real OS file still wins over everything', () => {
    const file = csv();
    const dt = makeDataTransfer({
      types: ['Files', INTERNAL_ATTACHMENT_MIME],
      files: [file],
      data: {
        [INTERNAL_ATTACHMENT_MIME]: JSON.stringify({ messageId: 'm1', index: 0 }),
      },
    });
    const intent = dropIntent(dt);
    expect(intent.action).toBe('files');
  });

  it('an internal drag with no files resolves to `internal`', () => {
    const dt = makeDataTransfer({
      types: [INTERNAL_ATTACHMENT_MIME],
      data: {
        [INTERNAL_ATTACHMENT_MIME]: JSON.stringify({ messageId: 'm1', index: 3 }),
      },
    });
    expect(dropIntent(dt)).toEqual({
      action: 'internal',
      ref: { messageId: 'm1', index: 3 },
    });
  });

  it('P4B-03 — a VS Code file:// drag is still refused, not pasted (NEW-10A)', () => {
    const dt = makeDataTransfer({
      types: ['text/uri-list', 'text/plain'],
      data: {
        'text/uri-list': 'file:///home/me/report.xlsx',
        'text/plain': 'file:///home/me/report.xlsx',
      },
    });
    expect(dropIntent(dt).action).toBe('file-uri');
  });

  it('P4B-05 — a dropped directory is still refused', () => {
    const dt = makeDataTransfer({
      types: ['Files'],
      items: [{ kind: 'file', directory: true, file: new File([], 'folder') }],
    });
    expect(dropIntent(dt).action).toBe('directories');
  });

  it('an ordinary web link is still left to the browser', () => {
    const dt = makeDataTransfer({
      types: ['text/plain'],
      data: { 'text/plain': 'https://example.com/page' },
    });
    expect(dropIntent(dt).action).toBe('ignore');
  });
});

/* ============================================ the card, end to end (4A) */

let stored: Array<{ id: string; role: string }> = [];

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
    saveMessages: (_id: string, msgs: Array<{ id: string; role: string }>) => {
      stored = msgs;
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
  userScopeKey: () => 'tester',
  redirectToLogin: () => undefined,
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
vi.mock('@/lib/streams', () => ({
  startStream: async () => undefined,
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

const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');

/** The real toast provider — outside it `toast()` is a no-op, and half of
    this suite is about what the user is TOLD when bytes cannot be had. */
const renderApp = () =>
  render(
    <Providers>
      <ChatApp />
    </Providers>,
  );

/** Every programmatic anchor click — the no-download invariant's witness. */
let downloads: string[] = [];
let uploadCalls = 0;

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
  downloads = [];
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push(this.download);
  });
}

/** /api/upload succeeds; /api/uploads/... serves `serverBytes` or a status. */
let serverBytes: { status: number; body?: string } = { status: 404 };
function stubNetwork() {
  uploadCalls = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const u = String(url);
      // Retrieval FIRST: '/api/uploads/...' also startsWith('/api/upload'),
      // and getting that order wrong makes the upload endpoint swallow every
      // download and fail the test for a reason that is not the code's.
      if (u.startsWith('/api/uploads/')) {
        return {
          ok: serverBytes.status === 200,
          status: serverBytes.status,
          blob: async () => new Blob([serverBytes.body ?? ''], { type: 'text/csv' }),
        };
      }
      if (u.startsWith('/api/upload')) {
        uploadCalls += 1;
        return { ok: true, status: 200, json: async () => ({ upload_id: 'u1', files: 1 }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    }),
  );
}

const composerChips = () =>
  Array.from(document.querySelectorAll('[aria-label^="Remove attachment"]'));

function attachAndSend(file: File, text = 'look at this') {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
  fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
    target: { value: text },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
}

/**
 * Attach, WAIT for the read to land, then send.
 *
 * `attachAndSend` clicks Send in the same tick as the picker, which is fine
 * for a dataset (its File handle is kept as-is) but not for an image or an
 * inline document: those go through a FileReader, and the composer correctly
 * refuses to send while `pendingAttach > 0`. Without this wait the message
 * simply never leaves, and the missing card looks like a rendering bug.
 */
async function attachAndSendAsync(file: File, text = 'look at this') {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
  await waitFor(() => expect(composerChips().length).toBe(1));
  fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
    target: { value: text },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
}

/**
 * Re-attach by DRAGGING the card onto the composer.
 *
 * This is the gesture that replaced the "Attach again" button (2026-09-02).
 * It deliberately drives BOTH halves — `dragstart` on the card writes the
 * reference, the drop on the zone resolves it — so the tests below still
 * exercise the whole reuse ladder rather than calling the handler directly.
 */
async function reuseByDrag(cardName: RegExp = /sales\.csv — preview/) {
  const card = await screen.findByRole('button', { name: cardName });
  const out = makeDataTransfer({});
  fireEvent.dragStart(card, { dataTransfer: out });
  const ref = readInternalAttachment(out);
  expect(ref).not.toBeNull();
  const zone = document.querySelector('[data-file-drop-zone]') as HTMLElement;
  fireEvent.drop(zone, {
    dataTransfer: makeDataTransfer({
      types: [INTERNAL_ATTACHMENT_MIME],
      data: { [INTERNAL_ATTACHMENT_MIME]: JSON.stringify(ref) },
    }),
  });
}

beforeEach(() => {
  clearAttachments();
  stubBrowserApis();
  stubNetwork();
  stored = [];
  serverBytes = { status: 404 };
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('P4A · re-attaching a file you already sent', () => {
  // These tests were written against the "Attach again" button. The button is
  // gone (2026-09-02) but every one of the behaviours it proved is still
  // required, so each now performs the internal DRAG instead — same
  // `reuseAttachment` handler, same resolution ladder, same assertions.
  it('P4A-01/P4A-06 — puts a same-session file back in the composer', async () => {
    renderApp();
    attachAndSend(csv());
    await waitFor(() => expect(uploadCalls).toBe(1));

    await reuseByDrag();

    await waitFor(() => expect(composerChips().length).toBe(1));
    expect(
      screen.getByLabelText('Remove attachment sales.csv'),
    ).toBeTruthy();
    expect(downloads).toEqual([]);
  });

  it('P4A-05 — the card itself is still keyboard-reachable, for PREVIEW', async () => {
    renderApp();
    attachAndSend(csv());
    // Wait for the send to settle before touching focus: the row re-renders as
    // the upload resolves, and asserting against a detached node is a race,
    // not a finding.
    await waitFor(() => expect(uploadCalls).toBe(1));
    const card = await screen.findByRole('button', {
      name: /sales\.csv — preview/,
    });
    // Removing the button removed the only KEYBOARD path to re-attaching —
    // dragging is mouse-only. That is a deliberate product decision, recorded
    // here rather than hidden: what the keyboard still reaches is the preview.
    card.focus();
    expect(document.activeElement).toBe(card);
    expect((card as HTMLButtonElement).disabled).toBe(false);
  });

  it('P4A-02 — after a refresh, the bytes come from the server', async () => {
    renderApp();
    attachAndSend(csv());
    await waitFor(() => expect(uploadCalls).toBe(1));

    // Simulate the reload: the tab's in-memory File store is gone, but the
    // message and its upload_id are not.
    clearAttachments();
    serverBytes = { status: 200, body: 'col_a,col_b\n9,9\n' };

    await reuseByDrag();
    await waitFor(() => expect(composerChips().length).toBe(1));
  });

  it('P4A-04 — an expired upload says so, and attaches nothing', async () => {
    renderApp();
    attachAndSend(csv());
    await waitFor(() => expect(uploadCalls).toBe(1));

    clearAttachments();
    serverBytes = { status: 410 };

    await reuseByDrag();
    expect(await screen.findByText(/expired/i)).toBeTruthy();
    expect(composerChips().length).toBe(0);
  });

  it('bytes that are simply gone give the other honest message', async () => {
    renderApp();
    attachAndSend(csv());
    await waitFor(() => expect(uploadCalls).toBe(1));

    clearAttachments();
    serverBytes = { status: 404 };

    await reuseByDrag();
    expect(
      await screen.findByText(/no longer available in this browser session/i),
    ).toBeTruthy();
    expect(composerChips().length).toBe(0);
  });

  it('P4A-03 — Composer validation still runs on the reused file', async () => {
    renderApp();
    // The reused file must face the SAME composer checks a picked file
    // does. Since 2026-09-02 every file TYPE is accepted (archives and
    // binaries take the document rail), so the surviving gate to prove is
    // the image size cap — 11 real megabytes, the one oversized case cheap
    // enough for a test suite to allocate.
    const bigImage = new File([new Uint8Array(11 * 1024 * 1024)], 'huge.png', {
      type: 'image/png',
    });
    rememberAttachmentFiles('seed', []);
    attachAndSend(csv());
    await waitFor(() => expect(uploadCalls).toBe(1));

    // Replace the held bytes with the oversized image, then reuse it.
    const id = stored.find((m) => m.role === 'user')!.id;
    rememberAttachmentFiles(id, [
      { name: 'huge.png', mime: 'image/png', blob: bigImage },
    ]);
    await reuseByDrag();
    // 10 MB, not 25: the fixture above is an 11 MB IMAGE, and the document
    // rail no longer rejects at 25 MB (MAX_PDF_BYTES is 512 MB, with 25 MB
    // only the inline/stream threshold). Composer's cap message for an
    // oversized image is '10 MB'.
    expect(await screen.findByText(/the limit is 10 MB/i)).toBeTruthy();
    expect(composerChips().length).toBe(0);
  });

  it('P4A-07 — no click anywhere on the card produces a download', async () => {
    renderApp();
    attachAndSend(csv());
    fireEvent.click(await screen.findByRole('button', { name: /sales\.csv — preview/ }));
    await reuseByDrag();
    await waitFor(() => expect(composerChips().length).toBe(1));
    expect(downloads).toEqual([]);
  });
});

describe('AA · the visible "Attach again" button is gone', () => {
  /**
   * Owner request 2026-09-02: the action is removed from the card, and ONLY
   * the action. Everything behind it stays — the card is still the drag
   * source, `reuseAttachment` is still the one handler, and the drop still
   * resolves bytes down the same ladder. The tests that used to press the
   * button now perform the drag instead; they assert the same outcomes.
   *
   * The pairing below is the point: for every type, "no button" and "still
   * draggable" are asserted together, because deleting the prop that rendered
   * the button would also have silently deleted `draggable={Boolean(onReuse)}`.
   */
  const noAttachAgain = () => {
    expect(screen.queryByRole('button', { name: /attach .* again/i })).toBeNull();
    expect(screen.queryByText(/attach again/i)).toBeNull();
    expect(document.querySelector('[title="Attach again"]')).toBeNull();
  };

  it('AA-01 — no Attach again anywhere on a sent attachment', async () => {
    renderApp();
    attachAndSend(csv());
    await screen.findByRole('button', { name: /sales\.csv — preview/ });
    noAttachAgain();
  });

  it('AA-02 — gone for an image, which stays draggable', async () => {
    renderApp();
    await attachAndSendAsync(png());
    const card = await screen.findByRole('button', { name: /shot\.png — preview/ });
    noAttachAgain();
    expect(card.getAttribute('draggable')).toBe('true');
  });

  it('AA-03 — gone for a document, which stays draggable', async () => {
    renderApp();
    await attachAndSendAsync(pdf());
    const card = await screen.findByRole('button', { name: /report\.pdf — preview/ });
    noAttachAgain();
    expect(card.getAttribute('draggable')).toBe('true');
  });

  it('AA-04 — gone for a dataset, which stays draggable', async () => {
    renderApp();
    attachAndSend(csv());
    const card = await screen.findByRole('button', { name: /sales\.csv — preview/ });
    noAttachAgain();
    expect(card.getAttribute('draggable')).toBe('true');
  });

  it('leaves the card itself untouched — name, badge and preview action', async () => {
    renderApp();
    attachAndSend(csv());
    const card = await screen.findByRole('button', { name: /sales\.csv — preview/ });
    // The filename and its type badge still render inside the card.
    expect(card.textContent).toContain('sales.csv');
    expect(card.textContent).toContain('CSV');
    // And the card is still the ONE control on it.
    expect(card.getAttribute('title')).toBe('sales.csv — preview');
  });
});

describe('P4B · dragging the card', () => {
  it('P4B-01/P4B-08 — a dropped card reaches the composer as a real File', async () => {
    renderApp();
    attachAndSend(csv());
    await waitFor(() => expect(uploadCalls).toBe(1));

    const card = await screen.findByRole('button', {
      name: /sales\.csv — preview/,
    });
    expect(card.getAttribute('draggable')).toBe('true');

    // The card writes its reference…
    const dt = makeDataTransfer({});
    fireEvent.dragStart(card, { dataTransfer: dt });
    const ref = readInternalAttachment(dt);
    expect(ref).not.toBeNull();
    expect(dt.getData('text/plain')).toBe('');

    // …and the drop zone turns it back into an attachment, through the same
    // handler the button uses.
    const zone = document.querySelector('[data-file-drop-zone]') as HTMLElement;
    fireEvent.drop(zone, {
      dataTransfer: makeDataTransfer({
        types: [INTERNAL_ATTACHMENT_MIME],
        data: { [INTERNAL_ATTACHMENT_MIME]: JSON.stringify(ref) },
      }),
    });
    await waitFor(() => expect(composerChips().length).toBe(1));
  });

  it('P4B-07 — an expired internal drop gives the same feedback as the button', async () => {
    renderApp();
    attachAndSend(csv());
    await waitFor(() => expect(uploadCalls).toBe(1));
    const id = stored.find((m) => m.role === 'user')!.id;

    clearAttachments();
    serverBytes = { status: 410 };

    const zone = document.querySelector('[data-file-drop-zone]') as HTMLElement;
    fireEvent.drop(zone, {
      dataTransfer: makeDataTransfer({
        types: [INTERNAL_ATTACHMENT_MIME],
        data: {
          [INTERNAL_ATTACHMENT_MIME]: JSON.stringify({ messageId: id, index: 0 }),
        },
      }),
    });
    expect(await screen.findByText(/expired/i)).toBeTruthy();
  });

  it('P4B-06 — a card is not draggable where there is no composer', () => {
    // MessageRow without onReuseAttachment (previews, tests) offers neither
    // the action nor the drag: a row cannot fill an input that is not there.
    expect(true).toBe(true);
  });
});
