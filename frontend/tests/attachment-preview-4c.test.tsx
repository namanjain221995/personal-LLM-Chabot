// @vitest-environment jsdom
/**
 * PHASE 4C — previews for the formats a browser cannot open.
 *
 * The rule that shapes all of this: `previewKindFor` is UNTOUCHED. It still
 * answers `none` for .xlsx, .docx, .zip and every executable format, because
 * it decides what can be rendered from BYTES and none of those can be. What
 * changed is that bytes stopped being the only source — the orchestrator
 * profiled the workbook and extracted the document's text when the file was
 * uploaded, and the dialog can ask it.
 *
 * That distinction is what keeps the security model intact, and it is what the
 * last block here pins: an executable format is not merely unhandled, it is
 * unreachable, because no loader is ever built for one.
 *
 * No package was added for any of this. Delimited text is parsed here (a small
 * specified problem), the workbook comes from stored JSON, and the DOCX text
 * comes from the stdlib extractor that already ran server-side.
 */

import {
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
  previewKindFor,
  rememberAttachmentFiles,
} from '@/lib/attachments';
import {
  parseDelimited,
  tableFromDelimited,
  workbookFromProfile,
  MAX_TABLE_ROWS,
} from '@/lib/previewData';
import type { ChatMessage } from '@/lib/types';

const UPLOAD = 'b'.repeat(32);
const CONV = 'conv-1';

/* ------------------------------------------------------------- fixtures */

/** The exact shape observed in the live `uploads.profile` column. */
const XLSX_PROFILE = [
  {
    file: 'Bug Fixing Status (1).xlsx',
    kind: 'spreadsheet',
    bytes: 55945,
    sheets: [
      {
        name: 'All Work Log',
        rows: 995,
        columns: [{ name: 'Date' }, { name: 'Bug-Id' }, { name: 'Status' }],
        sample_rows: [
          { Date: '2026-08-21', 'Bug-Id': 'H-07', Status: 'Fixed' },
          { Date: '2026-08-24', 'Bug-Id': 'H-03', Status: 'In Process' },
        ],
      },
      {
        name: 'Summary',
        rows: 2,
        columns: [{ name: 'Metric' }, { name: 'Value' }],
        full_rows: [
          { Metric: 'Open', Value: 3 },
          { Metric: 'Closed', Value: 12 },
        ],
      },
    ],
  },
];

const message = (over: Partial<ChatMessage> = {}): ChatMessage => ({
  id: 'm1',
  role: 'user',
  content: 'here you go',
  createdAt: 0,
  ...over,
});

const datasetMessage = (name: string): ChatMessage =>
  message({
    pdfName: name,
    meta: { attachments: [{ id: UPLOAD, name, kind: 'dataset' }] },
  });

function renderRow(m: ChatMessage, conversationId: string | null = CONV) {
  return render(
    <MessageRow
      message={m}
      isLast={false}
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      conversationId={conversationId}
    />,
  );
}

const card = (re: RegExp) => screen.getByRole('button', { name: re });

let downloads: string[] = [];
let requested: string[] = [];
let aborted = 0;

beforeEach(() => {
  clearAttachments();
  // jsdom implements neither, and the PDF/image paths mint one.
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: (b: Blob) => `blob:mock/${b.type || 'none'}`,
    revokeObjectURL: () => undefined,
  });
  downloads = [];
  requested = [];
  aborted = 0;
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    downloads.push(this.download);
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** Serve the profile listing and the document-text endpoint. */
function stubServer(opts: {
  profile?: unknown;
  status?: string;
  docText?: string;
  docStatus?: number;
  hang?: boolean;
}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: { signal?: AbortSignal }) => {
      const u = String(url);
      requested.push(u);
      if (opts.hang) {
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => {
            aborted += 1;
            reject(new Error('aborted'));
          });
        });
      }
      if (u.includes('/document')) {
        return {
          ok: (opts.docStatus ?? 200) === 200,
          status: opts.docStatus ?? 200,
          json: async () => ({ text: opts.docText ?? '', truncated: false }),
        };
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          uploads: [
            {
              id: UPLOAD,
              filename: 'Bug Fixing Status (1).xlsx',
              status: opts.status ?? 'ready',
              profile: opts.profile ?? XLSX_PROFILE,
            },
          ],
        }),
      };
    }),
  );
}

/* ================================================== pure parsing (CSV) */

describe('P4C · delimited parsing', () => {
  it('honours RFC 4180 quoting instead of splitting on commas', () => {
    const rows = parseDelimited(
      'name,note\n"Smith, J.","said ""hi"""\n"multi\nline",x\n',
      ',',
    );
    expect(rows).toEqual([
      ['name', 'note'],
      ['Smith, J.', 'said "hi"'],
      ['multi\nline', 'x'],
    ]);
  });

  it('stops early rather than walking a whole 200 MB file', () => {
    const many = 'a\n' + 'x\n'.repeat(5000);
    expect(parseDelimited(many, ',', 10).length).toBe(10);
  });

  it('P4C-03 — builds a bounded table and reports what it cut', () => {
    const body = 'a,b\n' + Array.from({ length: 500 }, (_, i) => `${i},${i}`).join('\n');
    const table = tableFromDelimited(body, 'big.csv')!;
    expect(table.columns).toEqual(['a', 'b']);
    expect(table.rows.length).toBe(MAX_TABLE_ROWS);
    expect(table.truncatedRows).toBe(true);
  });

  it('pads ragged rows rather than dropping them', () => {
    const table = tableFromDelimited('a,b,c\n1,2\n', 'r.csv')!;
    expect(table.rows[0]).toEqual(['1', '2', '']);
  });

  it('uses tabs for .tsv, commas otherwise', () => {
    expect(tableFromDelimited('a\tb\n1\t2\n', 'x.tsv')!.columns).toEqual(['a', 'b']);
    expect(tableFromDelimited('a,b\n1,2\n', 'x.csv')!.columns).toEqual(['a', 'b']);
  });
});

/* ================================================ pure profile reading */

describe('P4C · workbook from the stored profile', () => {
  it('reads sheets, columns and rows out of the real profile shape', () => {
    const wb = workbookFromProfile(XLSX_PROFILE, 'Bug Fixing Status (1).xlsx')!;
    expect(wb.sheets.map((s) => s.name)).toEqual(['All Work Log', 'Summary']);
    expect(wb.sheets[0].columns).toEqual(['Date', 'Bug-Id', 'Status']);
    expect(wb.sheets[0].rows).toBe(995);
  });

  it('P4C-06 — distinguishes a SAMPLE from the complete sheet', () => {
    const wb = workbookFromProfile(XLSX_PROFILE, 'Bug Fixing Status (1).xlsx')!;
    // 2 sample rows out of 995 — must never be presented as the whole sheet.
    expect(wb.sheets[0].complete).toBe(false);
    expect(wb.sheets[0].previewRows.length).toBe(2);
    // full_rows IS the sheet.
    expect(wb.sheets[1].complete).toBe(true);
  });

  it('returns null rather than throwing on a malformed profile', () => {
    for (const bad of [null, undefined, 42, 'text', [], [{ kind: 'text' }], [{}]]) {
      expect(workbookFromProfile(bad, 'x.xlsx')).toBeNull();
    }
  });
});

/* ==================================================== the dialog (XLSX) */

describe('P4C-05 · the XLSX modal', () => {
  it('shows sheet tabs, the selected sheet and its rows', async () => {
    stubServer({});
    renderRow(datasetMessage('Bug Fixing Status (1).xlsx'));
    fireEvent.click(card(/Bug Fixing Status/));

    const d = await screen.findByRole('dialog');
    await waitFor(() => expect(d.querySelector('table')).toBeTruthy());

    expect(screen.getByRole('tab', { name: 'All Work Log' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Summary' })).toBeTruthy();
    const headers = Array.from(d.querySelectorAll('th')).map((h) => h.textContent);
    expect(headers).toEqual(['Date', 'Bug-Id', 'Status']);
    expect(d.textContent).toContain('H-07');
    expect(downloads).toEqual([]);
  });

  it('P4C-06 — labels a sample honestly, and a complete sheet honestly', async () => {
    stubServer({});
    renderRow(datasetMessage('Bug Fixing Status (1).xlsx'));
    fireEvent.click(card(/Bug Fixing Status/));
    const d = await screen.findByRole('dialog');

    await waitFor(() =>
      expect(d.textContent).toContain('Showing 2 preview rows of 995 rows'),
    );

    fireEvent.click(screen.getByRole('tab', { name: 'Summary' }));
    await waitFor(() =>
      expect(d.textContent).toContain('the complete sheet'),
    );
  });

  it('switching sheets shows that sheet, not the first one', async () => {
    stubServer({});
    renderRow(datasetMessage('Bug Fixing Status (1).xlsx'));
    fireEvent.click(card(/Bug Fixing Status/));
    const d = await screen.findByRole('dialog');
    await waitFor(() => expect(d.querySelector('table')).toBeTruthy());

    fireEvent.click(screen.getByRole('tab', { name: 'Summary' }));
    await waitFor(() =>
      expect(
        Array.from(d.querySelectorAll('th')).map((h) => h.textContent),
      ).toEqual(['Metric', 'Value']),
    );
  });

  it('falls back to the honest card when the profile cannot be had', async () => {
    // Malformed, not expired: a swept upload KEEPS its profile — the TTL takes
    // the bytes, and the profile is a database row that outlives them.
    stubServer({ profile: [{ kind: 'text' }] });
    // Bytes present, so the fallback under test is "no usable profile", not
    // "no file" — those are different sentences and both are correct.
    rememberAttachmentFiles('m1', [
      { name: 'Bug Fixing Status (1).xlsx', mime: '', blob: new Blob(['PK']) },
    ]);
    renderRow(datasetMessage('Bug Fixing Status (1).xlsx'));
    fireEvent.click(card(/Bug Fixing Status/));
    expect(
      await screen.findByText(/Preview is not available for this file type/i),
    ).toBeTruthy();
    expect(downloads).toEqual([]);
  });

  it('asks for nothing at all when the row has no conversation', async () => {
    stubServer({});
    renderRow(datasetMessage('Bug Fixing Status (1).xlsx'), null);
    fireEvent.click(card(/Bug Fixing Status/));
    await screen.findByRole('dialog');
    expect(requested).toEqual([]);
  });
});

/* ==================================================== the dialog (DOCX) */

describe('P4C-07/08 · the DOCX modal', () => {
  const DOC = message({
    pdfName: 'spec.docx',
    meta: { attachments: [{ name: 'spec.docx', kind: 'pdf' }] },
  });

  it('shows the text the server already extracted', async () => {
    stubServer({ docText: 'Heading\n\nSome body text.' });
    renderRow(DOC);
    fireEvent.click(card(/spec\.docx/));
    const d = await screen.findByRole('dialog');
    await waitFor(() => expect(d.textContent).toContain('Some body text.'));
    expect(requested.some((u) => u.includes('/document?name=spec.docx'))).toBe(true);
    expect(downloads).toEqual([]);
  });

  it('P4C-08 — document content never becomes markup', async () => {
    stubServer({
      docText: '<img src=x onerror=alert(1)><script>alert(2)</script> plain',
    });
    renderRow(DOC);
    fireEvent.click(card(/spec\.docx/));
    const d = await screen.findByRole('dialog');
    await waitFor(() => expect(d.textContent).toContain('plain'));

    // The angle brackets survive as TEXT, and produced no elements.
    expect(d.querySelector('script')).toBeNull();
    expect(d.querySelector('img')).toBeNull();
    expect(d.textContent).toContain('<script>');
    expect(d.innerHTML).not.toContain('<script>');
  });

  it('falls back to the honest card when there is no stored text', async () => {
    stubServer({ docStatus: 404 });
    rememberAttachmentFiles('m1', [
      { name: 'spec.docx', mime: '', blob: new Blob(['PK']) },
    ]);
    renderRow(DOC);
    fireEvent.click(card(/spec\.docx/));
    expect(
      await screen.findByText(/Preview is not available for this file type/i),
    ).toBeTruthy();
  });
});

/* ========================================== legacy + unsupported formats */

describe('P4C-09 · what must still never render', () => {
  it('gives .xls and .doc a specific, actionable message', async () => {
    for (const [name, advice] of [
      ['old.xls', '.xlsx'],
      ['old.doc', '.docx'],
    ] as const) {
      stubServer({});
      renderRow(message({ pdfName: name }));
      fireEvent.click(card(new RegExp(name.replace('.', '\\.'))));
      const d = await screen.findByRole('dialog');
      expect(d.textContent).toMatch(/Legacy/i);
      expect(d.textContent).toContain(advice);
      cleanup();
    }
    expect(downloads).toEqual([]);
  });

  it('builds NO loader for an executable or archive format', async () => {
    for (const name of ['evil.svg', 'page.html', 'app.js', 'bundle.zip', 'f.parquet']) {
      stubServer({});
      renderRow(message({ pdfName: name }));
      fireEvent.click(card(new RegExp(name.replace('.', '\\.'))));
      await screen.findByRole('dialog');
      // Not merely unhandled — unreachable: nothing was even asked for.
      expect(requested).toEqual([]);
      expect(previewKindFor(name)).toBe('none');
      cleanup();
    }
    expect(downloads).toEqual([]);
  });

  it('the byte classifier is unchanged by any of this', () => {
    expect(previewKindFor('sales.xlsx')).toBe('none');
    expect(previewKindFor('spec.docx')).toBe('none');
    expect(previewKindFor('shot.png')).toBe('image');
    expect(previewKindFor('report.pdf')).toBe('pdf');
    expect(previewKindFor('data.csv')).toBe('text');
    expect(previewKindFor('evil.svg')).toBe('none');
    expect(previewKindFor('shot.png', 'text/html')).toBe('image');
  });
});

/* ================================================== lifecycle guarantees */

describe('P4C-10/11/12 · lifecycle', () => {
  it('P4C-11 — closing the dialog aborts a pending preview fetch', async () => {
    stubServer({ hang: true });
    renderRow(datasetMessage('Bug Fixing Status (1).xlsx'));
    fireEvent.click(card(/Bug Fixing Status/));
    await screen.findByRole('dialog');
    await waitFor(() => expect(requested.length).toBe(1));

    fireEvent.click(screen.getByRole('button', { name: 'Close preview' }));
    await waitFor(() => expect(aborted).toBe(1));
  });

  it('re-rendering the row does NOT refetch the preview', async () => {
    // The loaders are closures rebuilt on every render of the owning row. If
    // the effect depended on them, an open dialog would refetch once per
    // streamed token — so it depends on whether a loader EXISTS, not on which
    // closure it happens to be this time.
    stubServer({});
    const m = datasetMessage('Bug Fixing Status (1).xlsx');
    const { rerender } = renderRow(m);
    fireEvent.click(card(/Bug Fixing Status/));
    await screen.findByRole('dialog');
    await waitFor(() => expect(requested.length).toBe(1));

    for (let i = 0; i < 3; i += 1) {
      rerender(
        <MessageRow
          message={m}
          isLast={false}
          onRegenerate={vi.fn()}
          onRetry={vi.fn()}
          conversationId={CONV}
        />,
      );
    }
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy());
    expect(requested.length).toBe(1);
  });

  it('P4C-10 — an object URL is still revoked when the preview closes', async () => {
    const created: string[] = [];
    const revoked: string[] = [];
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: (b: Blob) => {
        const u = `blob:mock/${created.length}/${b.type}`;
        created.push(u);
        return u;
      },
      revokeObjectURL: (u: string) => revoked.push(u),
    });
    rememberAttachmentFiles('m1', [
      { name: 'report.pdf', mime: 'application/pdf', blob: new Blob(['%PDF-1.4']) },
    ]);
    renderRow(message({ pdfName: 'report.pdf' }));
    fireEvent.click(card(/report\.pdf/));
    await screen.findByRole('dialog');
    expect(created.length).toBe(1);

    fireEvent.click(screen.getByRole('button', { name: 'Close preview' }));
    await waitFor(() => expect(revoked).toEqual(created));
  });

  it('P4C-01/02 — PDF and images are untouched by 4C', async () => {
    rememberAttachmentFiles('m1', [
      { name: 'report.pdf', mime: 'application/pdf', blob: new Blob(['%PDF-1.4']) },
    ]);
    renderRow(message({ pdfName: 'report.pdf' }));
    fireEvent.click(card(/report\.pdf/));
    const d = await screen.findByRole('dialog');
    expect(d.querySelector('object')).toBeTruthy();
    expect(requested).toEqual([]);
    expect(downloads).toEqual([]);
  });

  it('P4C-12 — no preview of any kind starts a download', async () => {
    for (const name of ['Bug Fixing Status (1).xlsx', 'spec.docx', 'old.xls']) {
      stubServer({ docText: 'text' });
      renderRow(datasetMessage(name));
      fireEvent.click(card(new RegExp(name.replace(/[.()]/g, '\\$&'))));
      await screen.findByRole('dialog');
      cleanup();
    }
    expect(downloads).toEqual([]);
    expect(document.querySelector('a[download]')).toBeNull();
  });
});
