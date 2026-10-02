// @vitest-environment jsdom
/**
 * Opening a sent file that is not a photo (2026-10-02, chat media,
 * docs/chat-media/CONTRACT.md §10 "Other kinds").
 *
 *   · a video or audio file PLAYS, in the browser's own player, from the
 *     uploads file URL. Opening it fetches nothing: it used to download the
 *     whole file (up to 4 GB) into a Blob and then say "Preview is not
 *     available for this file type", on every device;
 *   · a format the dialog cannot draw from bytes (.zip, .pptx, .html) is not
 *     downloaded just to say so;
 *   · a PDF or text document whose bytes are gone still shows the text the
 *     chat read from it, and says the file itself is not there;
 *   · a swept workbook says it EXPIRED, instead of "This file is no longer
 *     available in this browser session" — no browser session swept it.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MessageRow } from '@/components/MessageRow';
import { clearAttachments, rememberAttachmentFiles, streamedPlayerFor } from '@/lib/attachments';
import type { ChatMessage, MessageAttachment } from '@/lib/types';

const CONV = 'conv-files-1';
const VIDEO = 'a'.repeat(32);
const AUDIO = 'b'.repeat(32);
const DOC = 'c'.repeat(32);
const SHEET = 'd'.repeat(32);

const turn = (attachments: MessageAttachment[], over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'u1',
  role: 'user',
  content: 'have a look',
  createdAt: 0,
  pdfName: attachments[0]?.name,
  meta: { attachments },
  ...over,
});

function renderRow(message: ChatMessage, conversationId: string | null = CONV) {
  return render(
    <MessageRow
      message={message}
      isLast={false}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      conversationId={conversationId}
    />,
  );
}

const card = (re: RegExp) => screen.getByRole('button', { name: re });

/** Every request the page made, and what each one answers. */
let requested: Array<{ url: string; range: string | null }> = [];
type Answer = { status: number; body?: unknown; blob?: Blob };
let answers: Array<[RegExp, Answer]> = [];

function serve(...routes: Array<[RegExp, Answer]>) {
  answers = routes;
}

beforeEach(() => {
  clearAttachments();
  requested = [];
  answers = [];
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: (b: Blob) => `blob:mock/${b.type || 'none'}`,
    revokeObjectURL: () => undefined,
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input);
      // Read as sent: jsdom's Headers silently drops `range`.
      const sent = (init?.headers ?? {}) as Record<string, string>;
      requested.push({ url, range: sent.range ?? null });
      const hit = answers.find(([re]) => re.test(url));
      if (!hit) return new Response('{}', { status: 599 });
      const [, a] = hit;
      if (a.blob) return new Response(a.blob, { status: a.status });
      return new Response(JSON.stringify(a.body ?? {}), {
        status: a.status,
        headers: { 'content-type': 'application/json' },
      });
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/* ============================================================ players */

describe('a video or audio file plays from the server', () => {
  it('a video opens in a <video> whose src is the uploads file URL, and nothing is fetched', async () => {
    renderRow(turn([{ name: 'site walk.mp4', kind: 'video', id: VIDEO }]));
    fireEvent.click(card(/site walk\.mp4/));
    const dialog = await screen.findByRole('dialog');
    const video = dialog.querySelector('video')!;
    expect(video).toBeTruthy();
    expect(video.getAttribute('src')).toBe(`/api/uploads/${CONV}/${VIDEO}/file`);
    expect(video.hasAttribute('controls')).toBe(true);
    // The header and the first frame on open, never the file.
    expect(video.getAttribute('preload')).toBe('metadata');
    // NEW-09A: not even the player's own menu offers to save it.
    expect(video.getAttribute('controlslist')).toBe('nodownload');
    expect(video.hasAttribute('playsinline')).toBe(true);
    expect(video.getAttribute('aria-label')).toBe('Video: site walk.mp4');
    expect(requested).toEqual([]);
    expect(dialog.textContent).not.toMatch(/Preview is not available/);
  });

  it('an audio file, which travels on the video rail, opens in an <audio>', async () => {
    renderRow(turn([{ name: 'memo.m4a', kind: 'video', id: AUDIO }]));
    fireEvent.click(card(/memo\.m4a/));
    const dialog = await screen.findByRole('dialog');
    const audio = dialog.querySelector('audio')!;
    expect(audio.getAttribute('src')).toBe(`/api/uploads/${CONV}/${AUDIO}/file`);
    expect(audio.getAttribute('preload')).toBe('metadata');
    expect(audio.getAttribute('controlslist')).toBe('nodownload');
    expect(dialog.querySelector('video')).toBeNull();
    expect(requested).toEqual([]);
  });

  it('several files on one turn: each card plays its own file', async () => {
    renderRow(
      turn([
        { name: 'brief.pdf', kind: 'pdf', id: DOC },
        { name: 'site walk.mp4', kind: 'video', id: VIDEO },
        { name: 'memo.mp3', kind: 'video', id: AUDIO },
      ]),
    );
    fireEvent.click(card(/memo\.mp3/));
    let dialog = await screen.findByRole('dialog');
    expect(dialog.querySelector('audio')!.getAttribute('src')).toBe(`/api/uploads/${CONV}/${AUDIO}/file`);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close preview' }));
    fireEvent.click(card(/site walk\.mp4/));
    dialog = await screen.findByRole('dialog');
    expect(dialog.querySelector('video')!.getAttribute('src')).toBe(`/api/uploads/${CONV}/${VIDEO}/file`);
    expect(requested).toEqual([]);
  });

  it('a file the video rail carried plays as a video even when its name says nothing', () => {
    const upload = { conversationId: CONV, uploadId: VIDEO };
    expect(streamedPlayerFor('clip', upload, 'video')?.kind).toBe('video');
    expect(streamedPlayerFor('clip', upload, 'pdf')).toBeNull();
    // No server copy yet: nothing to play from, and no guess.
    expect(streamedPlayerFor('clip.mp4', null, 'video')).toBeNull();
    // A playlist is text, never media (B12).
    expect(streamedPlayerFor('list.m3u', upload)).toBeNull();
  });

  it('a video with no upload id yet is not fetched either', async () => {
    renderRow(turn([{ name: 'site walk.mp4', kind: 'video' }]));
    fireEvent.click(card(/site walk\.mp4/));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.querySelector('video')).toBeNull();
    expect(requested).toEqual([]);
  });
});

describe('when a player cannot play, the server is asked once why', () => {
  async function failingPlayer(answer: Answer, name = 'site walk.mp4') {
    serve([/\/file$/, answer]);
    renderRow(turn([{ name, kind: 'video', id: VIDEO }]));
    fireEvent.click(card(new RegExp(name.replace('.', '\\.'))));
    const dialog = await screen.findByRole('dialog');
    fireEvent.error(dialog.querySelector('video')!);
    return dialog;
  }

  it('410: the upload expired', async () => {
    const dialog = await failingPlayer({ status: 410 });
    await waitFor(() => expect(dialog.textContent).toMatch(/This upload has expired and is no longer stored/));
    // One byte, not the file.
    expect(requested).toEqual([{ url: `/api/uploads/${CONV}/${VIDEO}/file`, range: 'bytes=0-0' }]);
  });

  it('404: not on the server', async () => {
    const dialog = await failingPlayer({ status: 404 });
    await waitFor(() => expect(dialog.textContent).toMatch(/This file is no longer on the server/));
  });

  it('the bytes arrive but will not play: the format, named', async () => {
    const dialog = await failingPlayer({ status: 206, blob: new Blob(['x']) }, 'scan.mkv');
    await waitFor(() => expect(dialog.textContent).toMatch(/This browser can’t play MKV files/));
  });

  it('the server cannot be reached', async () => {
    const dialog = await failingPlayer({ status: 502 });
    await waitFor(() => expect(dialog.textContent).toMatch(/couldn’t be loaded from the server/));
  });

  it('asks only once, however many errors the element raises', async () => {
    const dialog = await failingPlayer({ status: 410 });
    await waitFor(() => expect(dialog.textContent).toMatch(/expired/));
    expect(requested).toHaveLength(1);
  });
});

/* =============================================== no pointless download */

describe('a file with no byte preview is not downloaded to say so', () => {
  it.each(['deck.pptx', 'bundle.zip', 'page.html', 'table.parquet', 'notes.rtf'])(
    '%s: no request, the honest card',
    async (name) => {
      renderRow(turn([{ name, kind: 'pdf', id: DOC }]));
      fireEvent.click(card(new RegExp(name.replace('.', '\\.'))));
      const dialog = await screen.findByRole('dialog');
      expect(dialog.textContent).toMatch(/Preview is not available for this file type/);
      expect(requested).toEqual([]);
    },
  );
});

/* ============================================== bytes gone, text kept */

describe('a document whose bytes are gone still shows the text the chat read', () => {
  it('a swept PDF (410) shows its stored text under an "expired" line', async () => {
    serve(
      [/\/file$/, { status: 410 }],
      [/\/document\?name=/, { status: 200, body: { text: 'Quarterly revenue rose 12%.', truncated: false } }],
    );
    renderRow(turn([{ name: 'Q3 report.pdf', kind: 'pdf', id: DOC }]));
    fireEvent.click(card(/Q3 report\.pdf/));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(dialog.textContent).toContain('Quarterly revenue rose 12%.'));
    expect(dialog.textContent).toContain(
      'The file itself has expired and is no longer stored. This is the text the chat read from it.',
    );
    expect(requested.map((r) => r.url)).toEqual([
      `/api/uploads/${CONV}/${DOC}/file`,
      `/api/uploads/${CONV}/document?name=Q3%20report.pdf`,
    ]);
  });

  it('a PDF that never reached the server shows its text, without calling it expired', async () => {
    // A small document rides inside the chat request; its upload is best
    // effort. On another device this turn has a name and no upload id.
    serve([/\/document\?name=/, { status: 200, body: { text: 'Inline only.', truncated: false } }]);
    renderRow(turn([{ name: 'note.pdf', kind: 'pdf' }]));
    fireEvent.click(card(/note\.pdf/));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(dialog.textContent).toContain('Inline only.'));
    expect(dialog.textContent).toContain('The file itself can’t be opened here.');
    expect(dialog.textContent).not.toMatch(/expired/);
  });

  it('a swept document with no stored text still says it expired', async () => {
    serve([/\/file$/, { status: 410 }], [/\/document\?name=/, { status: 404 }]);
    renderRow(turn([{ name: 'Q3 report.pdf', kind: 'pdf', id: DOC }]));
    fireEvent.click(card(/Q3 report\.pdf/));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() =>
      expect(dialog.textContent).toMatch(/This upload has expired and is no longer stored/),
    );
  });

  it('a PDF this tab still holds renders from its bytes and asks for no text', async () => {
    rememberAttachmentFiles('u1', [
      { name: 'Q3 report.pdf', mime: 'application/pdf', blob: new Blob(['%PDF-1.4']) },
    ]);
    renderRow(turn([{ name: 'Q3 report.pdf', kind: 'pdf', id: DOC }]));
    fireEvent.click(card(/Q3 report\.pdf/));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.querySelector('object')).toBeTruthy();
    expect(requested).toEqual([]);
  });

  it('a .docx reads as its text with no "file is gone" line: that IS its preview', async () => {
    serve([/\/document\?name=/, { status: 200, body: { text: 'Body text.', truncated: false } }]);
    renderRow(turn([{ name: 'spec.docx', kind: 'pdf', id: DOC }]));
    fireEvent.click(card(/spec\.docx/));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(dialog.textContent).toContain('Body text.'));
    expect(dialog.textContent).not.toMatch(/The file itself/);
    // Text only: the .docx bytes were never fetched.
    expect(requested.map((r) => r.url)).toEqual([`/api/uploads/${CONV}/document?name=spec.docx`]);
  });

  it('a swept CSV dataset shows the table the server profiled', async () => {
    serve(
      [/\/file$/, { status: 410 }],
      [
        new RegExp(`/api/uploads/${CONV}$`),
        {
          status: 200,
          body: {
            uploads: [
              {
                id: SHEET,
                filename: 'sales.csv',
                status: 'expired',
                profile: [
                  {
                    file: 'sales.csv',
                    kind: 'table',
                    rows: 995,
                    columns: [{ name: 'region' }, { name: 'amount' }],
                    sample_rows: [{ region: 'north', amount: 10 }],
                  },
                ],
              },
            ],
          },
        },
      ],
    );
    renderRow(turn([{ name: 'sales.csv', kind: 'dataset', id: SHEET }]));
    fireEvent.click(card(/sales\.csv/));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(dialog.textContent).toContain('north'));
    expect(dialog.textContent).toContain(
      'The file itself has expired and is no longer stored. This is the summary the chat made of it.',
    );
    expect(dialog.textContent).toContain('Showing 1 preview row of 995 rows');
  });
});

/* ======================================================= the workbook */

describe('a swept workbook says so honestly', () => {
  const listing = (profile: unknown) =>
    [
      new RegExp(`/api/uploads/${CONV}$`),
      { status: 200, body: { uploads: [{ id: SHEET, filename: 'plan.xlsx', status: 'expired', profile }] } },
    ] as [RegExp, Answer];

  it('its stored summary still previews, under the expired line', async () => {
    serve(
      listing([
        {
          file: 'plan.xlsx',
          kind: 'spreadsheet',
          sheets: [{ name: 'Q3', rows: 2, columns: [{ name: 'item' }], full_rows: [{ item: 'rent' }, { item: 'pay' }] }],
        },
      ]),
    );
    renderRow(turn([{ name: 'plan.xlsx', kind: 'dataset', id: SHEET }]));
    fireEvent.click(card(/plan\.xlsx/));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(dialog.textContent).toContain('rent'));
    expect(dialog.textContent).toContain('The file itself has expired and is no longer stored.');
  });

  it('with no usable summary it says EXPIRED, never "this browser session"', async () => {
    serve(listing([{ kind: 'text' }]));
    renderRow(turn([{ name: 'plan.xlsx', kind: 'dataset', id: SHEET }]));
    fireEvent.click(card(/plan\.xlsx/));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() =>
      expect(dialog.textContent).toMatch(/This upload has expired and is no longer stored/),
    );
    expect(dialog.textContent).not.toMatch(/browser session/);
    // The workbook's bytes were never asked for: its preview is the profile.
    expect(requested.map((r) => r.url)).toEqual([`/api/uploads/${CONV}`]);
  });
});
