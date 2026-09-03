// @vitest-environment jsdom
/**
 * Editing a turn that is nothing but files.
 *
 * THE BUG: `MessageRow` decided whether to show the user action row with
 * `Boolean(message.content.trim())`. Attach two documents, forget to type the
 * question, press Send — and the turn arrived with no Edit, so the prompt you
 * meant to write could not be added. The one turn that most needs Edit was the
 * one turn that did not have it.
 *
 * THE OTHER HALF of the requirement is structural: the actions belong to the
 * MESSAGE. Three files must produce one Edit, not three, and hovering any of
 * them must reveal the same row. That is a claim about the DOM — where the row
 * is rendered relative to the attachment loops — and it is asserted as one,
 * because jsdom computes no hover styling and a test that pretended otherwise
 * would be theatre.
 *
 * The last block runs the REAL edit through the REAL ChatApp, because "Edit is
 * on screen" is not the fix. The fix is that clicking it gets you an empty
 * editor, and that sending the prompt you type keeps the files on the turn.
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
import { MessageRow } from '@/components/MessageRow';
import type { ChatMessage } from '@/lib/types';

/* ====================================================================
   PART 1 — the row itself: one per message, whatever it carries.
   ==================================================================== */

const turn = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'u1',
  role: 'user',
  content: '',
  createdAt: 0,
  ...over,
});

/** Three documents on one turn — the shape from the bug report. */
const THREE_DOCS = turn({
  meta: {
    attachments: [
      { name: 'report-a.pdf', kind: 'pdf' as const },
      { name: 'report-b.pdf', kind: 'pdf' as const },
      { name: 'sales.xlsx', kind: 'pdf' as const },
    ],
  },
});

function renderRow(message: ChatMessage, props: Record<string, unknown> = {}) {
  return render(
    <MessageRow
      message={message}
      isLast={false}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      onEditStart={vi.fn()}
      onEditCancel={vi.fn()}
      onEditSubmit={vi.fn()}
      {...props}
    />,
  );
}

/** Every attachment card in the row — they all announce themselves as previews. */
const fileCards = () => screen.queryAllByRole('button', { name: /— preview$/ });
const editButtons = () => screen.queryAllByRole('button', { name: 'Edit message' });
const copyButtons = () => screen.queryAllByRole('button', { name: 'Copy message' });

afterEach(cleanup);

describe('EDIT-FILE · one action row per message', () => {
  it('EDIT-FILE-01 · a single attachment-only turn renders exactly one Edit', () => {
    renderRow(turn({ pdfName: 'report.pdf' }));
    expect(fileCards()).toHaveLength(1);
    expect(editButtons()).toHaveLength(1);
  });

  it('EDIT-FILE-07/11 · three files still render exactly ONE Edit', () => {
    renderRow(THREE_DOCS);
    expect(fileCards()).toHaveLength(3);
    // The claim the owner made twice: never one Edit per file.
    expect(editButtons()).toHaveLength(1);
    expect(copyButtons()).toHaveLength(0);
  });

  it('EDIT-FILE-17 · three images likewise', () => {
    renderRow(
      turn({
        imageDataUrls: [
          'data:image/png;base64,AAA',
          'data:image/png;base64,BBB',
          'data:image/png;base64,CCC',
        ],
      }),
    );
    expect(fileCards()).toHaveLength(3);
    expect(editButtons()).toHaveLength(1);
  });

  it('EDIT-FILE-08/09/10 · every file card sits inside the SAME hover group', () => {
    // This is what makes "hover any file, get the same row" true. jsdom
    // computes no hover styling, so the assertion is the structural fact the
    // styling rests on: one `group/msg` element, and all three cards plus the
    // action row underneath it.
    const { container } = renderRow(THREE_DOCS);
    const groups = container.querySelectorAll('.group\\/msg');
    expect(groups).toHaveLength(1);

    const group = groups[0];
    for (const card of fileCards()) {
      expect(group.contains(card)).toBe(true);
    }
    expect(group.contains(editButtons()[0])).toBe(true);
  });

  it('EDIT-FILE-11 · the row is a SIBLING of the cards, never inside one', () => {
    // Duplication is impossible only if the row is outside the attachment
    // loops. Checked directly: no Edit button may be a descendant of any
    // attachment card, which is exactly what `cards.map(... actions ...)`
    // would produce.
    renderRow(THREE_DOCS);
    const edit = editButtons()[0];
    for (const card of fileCards()) {
      expect(card.contains(edit)).toBe(false);
    }
  });

  it('reveals on hover AND on keyboard focus anywhere in the message', () => {
    // A file-only turn puts the file card in the tab order before the row.
    // Without the group form of focus-within, a keyboard user stands on the
    // card with the actions still invisible.
    const { container } = renderRow(THREE_DOCS);
    const row = container.querySelector('.group\\/msg .mt-1\\.5.flex.items-center')!;
    expect(row.className).toContain('group-hover/msg:opacity-100');
    expect(row.className).toContain('group-focus-within/msg:opacity-100');
  });

  it('EDIT-FILE-12 · attachment + text keeps BOTH actions, as before', () => {
    renderRow(turn({ content: 'Summarize this.', pdfName: 'report.pdf' }));
    expect(editButtons()).toHaveLength(1);
    expect(copyButtons()).toHaveLength(1);
    expect(fileCards()).toHaveLength(1);
  });

  it('EDIT-FILE-13 · a text-only turn is exactly what it was', () => {
    renderRow(turn({ content: 'What is machine learning?' }));
    expect(editButtons()).toHaveLength(1);
    expect(copyButtons()).toHaveLength(1);
    expect(fileCards()).toHaveLength(0);
  });

  it('EDIT-FILE-03 · the editor opens empty for a turn that had no text', () => {
    renderRow(THREE_DOCS, { editing: true });
    const box = screen.getByRole('textbox', { name: 'Edit your message' });
    expect((box as HTMLTextAreaElement).value).toBe('');
    // …and the files are still on screen while it is being written.
    expect(fileCards()).toHaveLength(3);
  });

  it('EDIT-FILE-04 · Send is dead until a prompt is typed, then live', () => {
    renderRow(THREE_DOCS, { editing: true });
    const send = screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    fireEvent.change(screen.getByRole('textbox', { name: 'Edit your message' }), {
      target: { value: 'Compare these documents.' },
    });
    expect(send.disabled).toBe(false);
  });

  it('EDIT-FILE-19 · no "Attach again" control came back with the row', () => {
    renderRow(THREE_DOCS, { onReuseAttachment: vi.fn() });
    expect(screen.queryByRole('button', { name: /Attach .* again/i })).toBeNull();
    expect(screen.queryByText(/attach again/i)).toBeNull();
    // …and the cards are still drag sources.
    for (const card of fileCards()) {
      expect(card.getAttribute('draggable')).toBe('true');
    }
  });
});

/* ====================================================================
   PART 2 — the real flow: attach, forget the prompt, edit, send.

   REAL ChatApp, REAL edit pipeline. Only `startStream` and the network are
   stubbed, so what the edit produced can be inspected: the stored turns, the
   version tree, and what generation was handed.
   ==================================================================== */

interface StreamCall {
  conversationId: string;
  turns: ChatMessage[];
  context: ChatMessage[];
  pdf?: string | null;
  pdfName?: string | null;
  images?: string[];
  dataset?: boolean;
}

let stored: ChatMessage[] = [];
const startStream = vi.fn<(opts: StreamCall) => Promise<void>>(
  async () => undefined,
);

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
    saveMessages: (_id: string, msgs: ChatMessage[]) => {
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

const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');

const pdf = (name: string) =>
  new File(['%PDF-1.4 fake'], name, { type: 'application/pdf' });

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
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (String(url).startsWith('/api/upload')) {
        return { ok: true, status: 200, json: async () => ({ upload_id: 'up-1', files: 1 }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    }),
  );
}

/**
 * Attach `files` and press Send WITHOUT typing anything — the bug's setup.
 *
 * Waits for the composer CHIP before clicking Send, and that wait is
 * load-bearing rather than defensive: the composer refuses a send outright
 * while `pendingAttach > 0`, so clicking as soon as the change event fires
 * silently posts nothing whenever the file read has not finished. That is what
 * made an earlier version of this suite flaky — it failed under parallel load
 * and passed alone, which is the signature of a race in the test, not in the
 * code. The chip is the composer's own "the file is ready" signal.
 */
async function sendFilesWithNoPrompt(files: File[]) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files } });
  });
  for (const f of files) {
    await screen.findByLabelText(`Remove attachment ${f.name}`);
  }
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  });
  await waitFor(() => expect(userTurns()).toHaveLength(1));
}

/** Open the inline editor, type `text`, send — the fix, exercised. */
async function editTurnTo(text: string) {
  await act(async () => {
    fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }));
  });
  const box = screen.getByRole('textbox', { name: 'Edit your message' });
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  });
  await waitFor(() => expect(userTurns()).toHaveLength(2));
  return box as HTMLTextAreaElement;
}

const userTurns = () => stored.filter((m) => m.role === 'user');

beforeEach(() => {
  stubBrowserApis();
  stored = [];
  startStream.mockClear();
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
  render(
    <Providers>
      <ChatApp />
    </Providers>,
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('EDIT-FILE · adding the prompt you forgot', () => {
  it('EDIT-FILE-02/05/06 · edit an attachment-only turn and the file stays', async () => {
    await sendFilesWithNoPrompt([pdf('report.pdf')]);
    const original = userTurns()[0];
    expect(original.content).toBe('');
    expect(original.pdfName).toBe('report.pdf');

    // The bug: this button did not exist.
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Edit message' }));
    });
    expect(
      (screen.getByRole('textbox', { name: 'Edit your message' }) as HTMLTextAreaElement)
        .value,
    ).toBe('');
    await act(async () => {
      fireEvent.change(screen.getByRole('textbox', { name: 'Edit your message' }), {
        target: { value: 'Summarize this file.' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    });

    await waitFor(() => expect(userTurns()).toHaveLength(2));
    const edited = userTurns()[1];

    // The prompt landed…
    expect(edited.content).toBe('Summarize this file.');
    // …the file came with it…
    expect(edited.pdfName).toBe('report.pdf');
    // …and the original is still there, as a sibling version rather than
    // something the edit overwrote.
    expect(userTurns()[0].content).toBe('');
    expect(edited.id).not.toBe(original.id);
    expect(edited.meta?.branch?.parent).toBe(original.meta?.branch?.parent);
    expect(edited.meta?.branch?.self).not.toBe(original.meta?.branch?.self);
  });

  it('EDIT-FILE-15 · the edited turn keeps the attachment metadata verbatim', async () => {
    await sendFilesWithNoPrompt([pdf('report.pdf')]);
    const before = structuredClone(userTurns()[0].meta?.attachments);

    await editTurnTo('Summarize this file.');
    // Inherited wholesale — names, kinds and the durable upload ids with them.
    expect(userTurns()[1].meta?.attachments).toEqual(before);
  });

  it('EDIT-FILE-16 · three documents survive the edit in their original order', async () => {
    await sendFilesWithNoPrompt([
      pdf('report-a.pdf'),
      pdf('report-b.pdf'),
      pdf('report-c.pdf'),
    ]);
    expect(userTurns()[0].meta?.attachments?.map((a) => a.name)).toEqual([
      'report-a.pdf',
      'report-b.pdf',
      'report-c.pdf',
    ]);

    // ONE Edit for three files, on the real screen.
    expect(screen.getAllByRole('button', { name: 'Edit message' })).toHaveLength(1);

    await editTurnTo('Compare these documents.');
    expect(userTurns()[1].meta?.attachments?.map((a) => a.name)).toEqual([
      'report-a.pdf',
      'report-b.pdf',
      'report-c.pdf',
    ]);
    expect(userTurns()[1].content).toBe('Compare these documents.');
  });

  it('the edit hands generation the edited turn, not the empty original', async () => {
    await sendFilesWithNoPrompt([pdf('report.pdf')]);
    startStream.mockClear();

    await editTurnTo('Summarize this file.');
    await waitFor(() => expect(startStream).toHaveBeenCalledTimes(1));
    const sent = startStream.mock.calls[0][0];
    const asked = sent.context[sent.context.length - 1];
    expect(asked.role).toBe('user');
    expect(asked.content).toBe('Summarize this file.');
    expect(asked.pdfName).toBe('report.pdf');
    // Storage keeps BOTH versions; only the path sent to the model is one.
    expect(sent.turns.filter((m) => m.role === 'user')).toHaveLength(2);
  });

  it('EDIT-FILE-22 · a quoted excerpt on the turn survives the edit too', async () => {
    // meta is inherited wholesale, so `selected_context` rides along with the
    // attachments. Asserted here because an edit that dropped it would send
    // the follow-up with no idea what it was following up on.
    await sendFilesWithNoPrompt([pdf('report.pdf')]);
    const original = userTurns()[0];
    original.meta = {
      ...(original.meta ?? {}),
      selected_context: { text: 'drift', messageId: 'x', sourceRole: 'assistant' },
    };

    await editTurnTo('Why?');
    expect(userTurns()[1].meta?.selected_context?.text).toBe('drift');
  });
});
