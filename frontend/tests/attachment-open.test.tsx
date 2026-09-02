// @vitest-environment jsdom
/**
 * NEW-09A — clicking an attachment PREVIEWS it. It never downloads it.
 *
 * The first NEW-09 fix made the card a real button, and then made the button
 * do the wrong thing. It split file types into "previewable" and "not", opened
 * the first group with `window.open(blobUrl)` and DOWNLOADED the second through
 * a synthesised `<a download>`. Manual testing in Chrome found both halves
 * wrong at once:
 *
 *   - a .docx/.xlsx/.zip card downloaded the file, which nobody asked it to do;
 *   - and navigating a tab to a blob: URL is itself a download instruction as
 *     far as Chrome is concerned whenever it cannot render the type inline, so
 *     even the "preview" path put files in the Downloads tray.
 *
 * There was a third download hiding in it: when a popup blocker ate the new
 * tab, the code fell back to downloading. A blocked preview silently becoming a
 * file on disk is the worst of the three.
 *
 * The contract is now absolute and this suite enforces it as an INVARIANT
 * rather than a per-case assertion: no click on any attachment card, of any
 * type, in any state, may produce a programmatic download or an external tab.
 * `expectNoDownloads()` runs on every interaction below, and the spy that backs
 * it watches HTMLAnchorElement.prototype.click, so it catches a download
 * however it is spelled.
 *
 * Previewing happens INSIDE the app, in a dialog we control — which is the
 * whole point. The browser's own blob navigation is not steerable; a dialog is.
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
import {
  clearAttachments,
  dataUrlToBlob,
  MAX_TEXT_PREVIEW_BYTES,
  previewKindFor,
  rememberAttachmentFiles,
  resolveAttachment,
} from '@/lib/attachments';
import type { ChatMessage } from '@/lib/types';

/* --------------------------------------------------------------- fixtures */

const PNG_DATA_URL =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';

const userMessage = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'u1',
  role: 'user',
  content: 'have a look at this',
  createdAt: 0,
  ...over,
});

function renderRow(message: ChatMessage) {
  return render(
    <MessageRow
      message={message}
      isLast={false}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
    />,
  );
}

/* ------------------------------------------------- browser API instruments */

/** Every programmatic anchor click, however it was spelled. */
let downloads: Array<{ href: string; download: string }> = [];
let created: string[] = [];
let revoked: string[] = [];
let openCalls: string[] = [];

function instrumentBrowser() {
  downloads = [];
  created = [];
  revoked = [];
  openCalls = [];
  let seq = 0;
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: (blob: Blob) => {
      const url = `blob:mock/${blob.type || 'none'}/${seq++}`;
      created.push(url);
      return url;
    },
    revokeObjectURL: (url: string) => {
      revoked.push(url);
    },
  });
  // A new tab is no longer part of the design; calling it at all is a failure.
  vi.stubGlobal(
    'open',
    vi.fn((url: string) => {
      openCalls.push(url);
      return {};
    }),
  );
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push({ href: this.href, download: this.download });
  });
}

/**
 * THE invariant. Attachment-card download count must be zero — always.
 *
 * Three independent witnesses, because one download can be written three ways:
 * a synthesised anchor that is clicked, an anchor left in the DOM carrying the
 * attribute, and a tab navigated to a blob the browser decides to save instead
 * of render.
 */
function expectNoDownloads() {
  expect(downloads).toEqual([]);
  expect(document.querySelectorAll('a[download]').length).toBe(0);
  expect(openCalls).toEqual([]);
}

const dialog = () => screen.queryByRole('dialog');
const card = (name: RegExp) => screen.getByRole('button', { name });

beforeEach(() => {
  instrumentBrowser();
  clearAttachments();
});

afterEach(async () => {
  // Let any in-flight text read settle into a still-mounted tree; resolving
  // after teardown surfaces as an unhandled React scheduler error.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  clearAttachments();
});

/** Store one file against message u1 and render the card that shows it. */
function withFile(name: string, mime: string, body: BlobPart = 'x') {
  rememberAttachmentFiles('u1', [
    { name, mime, blob: new Blob([body], { type: mime }) },
  ]);
}

/* ================================================ 1. every type PREVIEWS */

describe('clicking an attachment opens an in-app preview', () => {
  it('shows an image inside the dialog', async () => {
    // NEW09A-01
    withFile('shot.png', 'image/png');
    renderRow(userMessage({ imageDataUrl: PNG_DATA_URL }));

    fireEvent.click(card(/shot\.png/));

    const d = await screen.findByRole('dialog');
    const img = d.querySelector('img') as HTMLImageElement;
    expect(img.getAttribute('src')?.startsWith('blob:')).toBe(true);
    expectNoDownloads();
  });

  it('shows a PDF inside the dialog, with no download attribute anywhere', async () => {
    // NEW09A-02
    withFile('report.pdf', 'application/pdf', '%PDF-1.4');
    renderRow(userMessage({ pdfName: 'report.pdf' }));

    fireEvent.click(card(/report\.pdf/));

    const d = await screen.findByRole('dialog');
    const frame = d.querySelector('object, iframe, embed') as HTMLElement;
    expect(frame).toBeTruthy();
    const src = frame.getAttribute('data') ?? frame.getAttribute('src') ?? '';
    expect(src.startsWith('blob:')).toBe(true);
    expect(frame.hasAttribute('download')).toBe(false);
    expectNoDownloads();
  });

  it('offers an honest fallback when the PDF cannot be rendered', async () => {
    // NEW09A-03 — the <object> fallback the browser shows instead of saving it.
    withFile('report.pdf', 'application/pdf', '%PDF-1.4');
    renderRow(userMessage({ pdfName: 'report.pdf' }));

    fireEvent.click(card(/report\.pdf/));
    await screen.findByRole('dialog');

    expect(screen.getByText(/Preview could not be displayed/i)).toBeTruthy();
    expectNoDownloads();
  });

  it.each([
    ['notes.txt', 'text/plain', 'hello from a text file'],
    ['README.md', 'text/markdown', '# a heading\n\nbody text'],
    ['payload.json', 'application/json', '{"ok":true}'],
    ['events.jsonl', '', '{"a":1}\n{"a":2}'],
  ])('reads %s and shows its text in the dialog', async (name, mime, body) => {
    // NEW09A-04 … 09
    withFile(name, mime, body);
    renderRow(userMessage({ pdfName: name }));

    fireEvent.click(card(new RegExp(name.replace('.', '\\.'))));

    await screen.findByRole('dialog');
    // Compared against the raw textContent rather than through getByText:
    // the default matcher collapses whitespace, which would mangle the tab in
    // a .tsv fixture into something the file never contained.
    await waitFor(() =>
      expect(document.querySelector('pre')?.textContent).toBe(body),
    );
    expectNoDownloads();
  });

  it.each([
    ['sales.csv', 'text/csv', 'a,b\n1,2', ['a', 'b', '1', '2']],
    ['data.tsv', 'text/tab-separated-values', 'a\tb\n3\t4', ['a', 'b', '3', '4']],
  ])(
    // PHASE 4C changed this deliberately: delimited data now renders as a
    // TABLE. The old expectation was the raw text, which is technically the
    // file's contents and practically unreadable past four columns. The
    // no-download invariant below is unchanged and still the point.
    'renders %s as a table of its cells',
    async (name, mime, body, cells) => {
      withFile(name, mime, body);
      renderRow(userMessage({ pdfName: name }));

      fireEvent.click(card(new RegExp(name.replace('.', '\\.'))));
      const d = await screen.findByRole('dialog');

      // The dialog opens before the blob has been read — the table appears
      // when the text lands, so it is waited for rather than assumed.
      await waitFor(() => expect(d.querySelector('table')).toBeTruthy());
      // Headers come from the first row; the rest are cells.
      const headers = Array.from(d.querySelectorAll('th')).map((h) => h.textContent);
      const values = Array.from(d.querySelectorAll('td')).map((t) => t.textContent);
      expect([...headers, ...values]).toEqual(cells);
      expectNoDownloads();
    },
  );

  it('truncates a very large text file instead of rendering all of it', async () => {
    // NEW09A-10
    const huge = 'x'.repeat(MAX_TEXT_PREVIEW_BYTES + 5000);
    withFile('big.txt', 'text/plain', huge);
    renderRow(userMessage({ pdfName: 'big.txt' }));

    fireEvent.click(card(/big\.txt/));

    expect(await screen.findByText(/Preview truncated/i)).toBeTruthy();
    const pre = document.querySelector('pre') as HTMLElement;
    expect(pre.textContent!.length).toBeLessThanOrEqual(MAX_TEXT_PREVIEW_BYTES);
    expectNoDownloads();
  });

  it('never injects file content as markup', async () => {
    // NEW09A-11 — the text preview is React text, not innerHTML.
    withFile('evil.txt', 'text/plain', '<img src=x onerror=alert(1)>');
    renderRow(userMessage({ pdfName: 'evil.txt' }));

    fireEvent.click(card(/evil\.txt/));
    await screen.findByRole('dialog');

    expect(
      await screen.findByText('<img src=x onerror=alert(1)>'),
    ).toBeTruthy();
    expect(document.querySelectorAll('img').length).toBe(0);
    expectNoDownloads();
  });
});

/* ============================ 2. binary formats: a shell, NEVER a download */

describe('formats the browser cannot render', () => {
  it.each([
    ['spec.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
    ['sales.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
    ['bundle.zip', 'application/zip'],
    ['logs.tar.gz', 'application/gzip'],
    ['frame.parquet', ''],
    ['firmware.bin', 'application/octet-stream'],
  ])('opens a preview shell for %s and downloads nothing', async (name, mime) => {
    // NEW09A-12 … 17 — the manual bug, one case per format.
    withFile(name, mime, 'PK');
    renderRow(userMessage({ pdfName: name }));

    fireEvent.click(card(new RegExp(name.replace(/\./g, '\\.'))));

    // Checked FIRST and synchronously: the old code downloaded on this very
    // click, so asserting the dialog first would report a missing dialog and
    // bury the actual defect.
    expectNoDownloads();

    const d = await screen.findByRole('dialog');
    expect(d.textContent).toContain(name);
    expect(
      screen.getByText(/Preview is not available for this file type/i),
    ).toBeTruthy();
    expectNoDownloads();
  });

  it('names the type and size in the shell instead of just failing', async () => {
    // NEW09A-18
    withFile('spec.docx', 'application/msword', 'PK0123456789');
    renderRow(userMessage({ pdfName: 'spec.docx' }));

    fireEvent.click(card(/spec\.docx/));
    const d = await screen.findByRole('dialog');

    expect(d.textContent).toContain('DOCX');
    expect(d.textContent).toMatch(/\d+\s*(B|KB|MB)/);
    expectNoDownloads();
  });

  it('refuses to preview executable formats, and still does not download', async () => {
    // NEW09A-19 — SVG/HTML/JS never render; they also never save.
    for (const [name, mime] of [
      ['logo.svg', 'image/svg+xml'],
      ['page.html', 'text/html'],
      ['run.js', 'text/javascript'],
    ] as const) {
      cleanup();
      clearAttachments();
      withFile(name, mime, '<svg onload=alert(1)>');
      renderRow(userMessage({ pdfName: name }));

      fireEvent.click(card(new RegExp(name.replace('.', '\\.'))));
      await screen.findByRole('dialog');

      expect(
        screen.getByText(/Preview is not available for this file type/i),
      ).toBeTruthy();
      expect(document.querySelectorAll('object, iframe, embed').length).toBe(0);
      expectNoDownloads();
    }
  });
});

/* ============================================ 3. the invariant, stated once */

describe('attachment cards never download — the invariant', () => {
  it('creates no anchor with a download attribute for ANY type', async () => {
    // NEW09A-20
    const cases: Array<[string, string]> = [
      ['shot.png', 'image/png'],
      ['report.pdf', 'application/pdf'],
      ['notes.txt', 'text/plain'],
      ['sales.csv', 'text/csv'],
      ['spec.docx', 'application/msword'],
      ['sheet.xlsx', 'application/vnd.ms-excel'],
      ['bundle.zip', 'application/zip'],
      ['mystery.xyz', ''],
    ];
    for (const [name, mime] of cases) {
      cleanup();
      clearAttachments();
      withFile(name, mime, 'bytes');
      renderRow(userMessage({ pdfName: name }));
      fireEvent.click(card(new RegExp(name.replace('.', '\\.'))));
      expectNoDownloads();
      await screen.findByRole('dialog');
    }
    expect(downloads.length).toBe(0); // ATTACHMENT CARD DOWNLOAD COUNT = 0
  });

  it('opens no external tab, so a popup blocker can break nothing', async () => {
    // NEW09A-21 — there is no window.open path left to be blocked, so there is
    // no blocked-popup fallback either. That fallback WAS a download.
    withFile('report.pdf', 'application/pdf', '%PDF');
    renderRow(userMessage({ pdfName: 'report.pdf' }));

    fireEvent.click(card(/report\.pdf/));
    await screen.findByRole('dialog');

    expect(openCalls).toEqual([]);
    expect(downloads).toEqual([]);
  });

  it('does not force application/octet-stream on any previewed blob', async () => {
    // NEW09A-22 — octet-stream is how the old code MADE Chrome download.
    withFile('report.pdf', 'application/pdf', '%PDF');
    renderRow(userMessage({ pdfName: 'report.pdf' }));

    fireEvent.click(card(/report\.pdf/));
    await screen.findByRole('dialog');

    expect(created.some((u) => u.includes('octet-stream'))).toBe(false);
  });
});

/* ================================================= 4. object URL lifecycle */

describe('object URL lifecycle', () => {
  it('mints nothing until a card is clicked', () => {
    // NEW09A-23
    withFile('shot.png', 'image/png');
    renderRow(userMessage({ imageDataUrl: PNG_DATA_URL }));

    expect(created).toEqual([]);
  });

  it('keeps the URL alive for as long as the preview is open', async () => {
    // NEW09A-24 — revoking early is what produced blank previews before.
    withFile('shot.png', 'image/png');
    renderRow(userMessage({ imageDataUrl: PNG_DATA_URL }));

    fireEvent.click(card(/shot\.png/));
    await screen.findByRole('dialog');

    expect(created.length).toBe(1);
    expect(revoked).toEqual([]);
  });

  it('revokes the URL when the preview closes', async () => {
    // NEW09A-25 — tied to the dialog's life, not to a 60-second timer.
    withFile('shot.png', 'image/png');
    renderRow(userMessage({ imageDataUrl: PNG_DATA_URL }));

    fireEvent.click(card(/shot\.png/));
    await screen.findByRole('dialog');
    fireEvent.click(screen.getByRole('button', { name: /close preview/i }));

    await waitFor(() => expect(dialog()).toBe(null));
    expect(revoked).toEqual(created);
  });

  it('mints no URL at all for a format it will not render', async () => {
    // NEW09A-26
    withFile('bundle.zip', 'application/zip');
    renderRow(userMessage({ pdfName: 'bundle.zip' }));

    fireEvent.click(card(/bundle\.zip/));
    await screen.findByRole('dialog');

    expect(created).toEqual([]);
  });

  it('closes on Escape and still revokes', async () => {
    // NEW09A-27
    withFile('shot.png', 'image/png');
    renderRow(userMessage({ imageDataUrl: PNG_DATA_URL }));

    fireEvent.click(card(/shot\.png/));
    const d = await screen.findByRole('dialog');
    fireEvent.keyDown(d, { key: 'Escape' });

    await waitFor(() => expect(dialog()).toBe(null));
    expect(revoked).toEqual(created);
  });
});

/* ============================================ 5. bytes gone after a reload */

describe('when the bytes are gone', () => {
  it('opens the dialog and says to re-attach', async () => {
    // NEW09A-28
    renderRow(userMessage({ pdfName: 'report.pdf' }));

    fireEvent.click(card(/report\.pdf/));

    await screen.findByRole('dialog');
    expect(
      screen.getByText(/no longer available in this browser session/i),
    ).toBeTruthy();
    expect(screen.getByText(/Re-attach/i)).toBeTruthy();
  });

  it('opens no tab and downloads nothing', async () => {
    // NEW09A-29 / 30
    renderRow(userMessage({ pdfName: 'report.pdf' }));

    fireEvent.click(card(/report\.pdf/));
    await screen.findByRole('dialog');

    expectNoDownloads();
    expect(created).toEqual([]);
  });

  it('still previews an image, because its persisted preview IS the payload', async () => {
    // NEW09A-31
    renderRow(userMessage({ imageDataUrl: PNG_DATA_URL }));

    fireEvent.click(card(/image/i));

    const d = await screen.findByRole('dialog');
    expect(d.querySelector('img')).toBeTruthy();
    expectNoDownloads();
  });
});

/* =================================================== 6. semantics preserved */

describe('the card itself is unchanged apart from what it does', () => {
  it('is still a real button naming the file', () => {
    // NEW09A-32
    renderRow(userMessage({ pdfName: 'Q3 invoice.pdf' }));

    const el = card(/Q3 invoice\.pdf/);
    expect(el.tagName).toBe('BUTTON');
    expect(el.getAttribute('type')).toBe('button');
    expect(el.getAttribute('aria-label')).toContain('Q3 invoice.pdf');
  });

  it('promises a preview, not a download, in its accessible name', () => {
    // NEW09A-33 — the label must not still say "download".
    withFile('spec.docx', 'application/msword');
    renderRow(userMessage({ pdfName: 'spec.docx' }));

    const label = card(/spec\.docx/).getAttribute('aria-label') ?? '';
    expect(label.toLowerCase()).toContain('preview');
    expect(label.toLowerCase()).not.toContain('download');
  });

  it('keeps the filename, the badge and the icon', () => {
    // NEW09A-34
    renderRow(userMessage({ pdfName: 'report.pdf' }));

    expect(screen.getByText('report.pdf')).toBeTruthy();
    expect(screen.getByText('PDF')).toBeTruthy();
    expect(document.querySelector('svg')).toBeTruthy();
  });

  it('renders the message image exactly as before', () => {
    // NEW09A-35
    renderRow(userMessage({ imageDataUrls: [PNG_DATA_URL, PNG_DATA_URL] }));

    const imgs = document.querySelectorAll('img');
    expect(imgs.length).toBe(2);
    expect(imgs[0].getAttribute('src')).toBe(PNG_DATA_URL);
    expect(imgs[0].className).toContain('max-h-40');
  });

  it('never renders a filename as markup', () => {
    // NEW09A-36
    renderRow(userMessage({ pdfName: '<img src=x onerror=alert(1)>.pdf' }));

    expect(document.querySelectorAll('img').length).toBe(0);
    expect(screen.getByText('<img src=x onerror=alert(1)>.pdf')).toBeTruthy();
  });

  it('opens the preview from the keyboard', async () => {
    // NEW09A-37 — a native button gives Enter and Space for free.
    withFile('report.pdf', 'application/pdf', '%PDF');
    renderRow(userMessage({ pdfName: 'report.pdf' }));

    const el = card(/report\.pdf/);
    el.focus();
    expect(document.activeElement).toBe(el);
    fireEvent.click(el, { detail: 0 }); // what Enter/Space dispatch

    expect(await screen.findByRole('dialog')).toBeTruthy();
    expectNoDownloads();
  });
});

/* ======================================================= 7. identity + pure */

describe('attachment identity', () => {
  it('previews the second image of a turn, not the first', async () => {
    // NEW09A-38
    rememberAttachmentFiles('u1', [
      { name: 'one.png', mime: 'image/png', blob: new Blob(['1'], { type: 'image/png' }) },
      { name: 'two.png', mime: 'image/png', blob: new Blob(['2'], { type: 'image/png' }) },
    ]);
    renderRow(userMessage({ imageDataUrls: [PNG_DATA_URL, PNG_DATA_URL] }));

    fireEvent.click(card(/two\.png/));
    const d = await screen.findByRole('dialog');

    expect(d.textContent).toContain('two.png');
  });

  it('keys by message and index, never by filename', () => {
    // NEW09A-39 — two turns may both attach `invoice.pdf`.
    rememberAttachmentFiles('u1', [
      { name: 'invoice.pdf', mime: 'application/pdf', blob: new Blob(['FIRST']) },
    ]);
    rememberAttachmentFiles('u2', [
      { name: 'invoice.pdf', mime: 'application/pdf', blob: new Blob(['SECOND-LONGER']) },
    ]);

    expect(resolveAttachment('u1', 0).size).toBe(5);
    expect(resolveAttachment('u2', 0).size).toBe(13);
  });

  it('reports unavailable rather than inventing bytes', () => {
    // NEW09A-40
    const gone = resolveAttachment('nope', 0, { name: 'report.pdf' });
    expect(gone.blob).toBe(null);
    expect(gone.kind).toBe('unavailable');
  });
});

describe('the preview classifier', () => {
  it('routes each family to the right renderer', () => {
    // NEW09A-41
    expect(previewKindFor('a.png', '')).toBe('image');
    expect(previewKindFor('a.jpeg', '')).toBe('image');
    expect(previewKindFor('a.webp', '')).toBe('image');
    expect(previewKindFor('a.gif', '')).toBe('image');
    expect(previewKindFor('a.pdf', '')).toBe('pdf');
    expect(previewKindFor('a.txt', '')).toBe('text');
    expect(previewKindFor('a.md', '')).toBe('text');
    expect(previewKindFor('a.csv', '')).toBe('text');
    expect(previewKindFor('a.tsv', '')).toBe('text');
    expect(previewKindFor('a.json', '')).toBe('text');
    expect(previewKindFor('a.jsonl', '')).toBe('text');
    expect(previewKindFor('a.ndjson', '')).toBe('text');
    expect(previewKindFor('a.docx', '')).toBe('none');
    expect(previewKindFor('a.xlsx', '')).toBe('none');
    expect(previewKindFor('a.zip', '')).toBe('none');
    expect(previewKindFor('a.parquet', '')).toBe('none');
  });

  it('trusts the extension over an attacker-chosen MIME', () => {
    // NEW09A-42
    expect(previewKindFor('shot.png', 'text/html')).toBe('image');
    expect(previewKindFor('notes.txt', 'application/javascript')).toBe('text');
    expect(previewKindFor('logo.svg', 'image/png')).toBe('none');
    expect(previewKindFor('page.html', 'text/plain')).toBe('none');
  });

  it('falls back to the MIME only when the name says nothing', () => {
    // NEW09A-43
    expect(previewKindFor('scan', 'image/png')).toBe('image');
    expect(previewKindFor('scan', 'application/pdf')).toBe('pdf');
    expect(previewKindFor('scan', 'image/svg+xml')).toBe('none');
    expect(previewKindFor('scan', '')).toBe('none');
  });
});

describe('helpers', () => {
  it('decodes a data: URL into a typed blob', () => {
    // NEW09A-44
    const blob = dataUrlToBlob(PNG_DATA_URL);
    expect(blob?.type).toBe('image/png');
    expect((blob as Blob).size).toBeGreaterThan(0);
  });

  it('rejects anything that is not a base64 data URL', () => {
    // NEW09A-45
    expect(dataUrlToBlob('https://example.com/x.png')).toBe(null);
    expect(dataUrlToBlob('')).toBe(null);
    expect(dataUrlToBlob(undefined)).toBe(null);
  });
});
