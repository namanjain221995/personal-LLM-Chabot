/**
 * lib/myfiles.ts — the "My files" page's whole conversation with
 * GET /api/files/mine and /api/files/mine/summary, and the decisions it makes
 * about each row: what the badge says, which preview it gets, where Download
 * points, and how the filters live in the page URL.
 *
 * The response shapes are orchestrator/app/myfiles.py's (_item, list_files,
 * summarise, retention).
 */
import { describe, expect, it } from 'vitest';

import {
  AVAILABILITY_LABEL,
  BYTE_PREVIEW_MAX_BYTES,
  DEFAULT_FILTERS,
  KIND_FILTER_LABEL,
  KIND_LABEL,
  appendFiles,
  availabilityNote,
  chatUrl,
  dayStartIso,
  downloadUrl,
  filtersFromQuery,
  filtersToQuery,
  hasActiveFilters,
  listRequestUrl,
  nextDayStartIso,
  parseMyFilesPage,
  parseSummary,
  previewPlanFor,
  retentionSentences,
  rowDomId,
  summaryFromProfile,
  summaryRequestUrl,
  type Filters,
  type MyFile,
} from '@/lib/myfiles';

const HEX = 'a'.repeat(32);
const REC = 'b'.repeat(32);

function row(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: `upload:${HEX}`,
    source: 'upload',
    kind: 'document',
    name: 'Q3 report.pdf',
    bytes: 2_400_000,
    created_at: '2026-09-30T09:14:00+00:00',
    conversation: { id: 'conv-1', title: 'Quarterly planning' },
    availability: 'available',
    media: null,
    can: { download: true, preview: null, delete: false },
    ...over,
  };
}

const RETENTION = {
  upload_hours: 24,
  recording_days: 0,
  video_kept_with_chat: true,
  video_grace_hours: 72,
  pictures: 'browser_only',
  picture_memory_hours: 2,
};

function parsed(over: Record<string, unknown> = {}): MyFile {
  const page = parseMyFilesPage({ items: [row(over)], next_cursor: null, retention: RETENTION });
  expect(page?.items).toHaveLength(1);
  return page!.items[0]!;
}

describe('parsing a page', () => {
  it('keeps well-formed rows and maps every field', () => {
    const page = parseMyFilesPage({
      items: [
        row(),
        row({
          id: 'text:42',
          source: 'text',
          name: 'inline-only.docx',
          bytes: null,
          availability: 'text_only',
          can: { download: false, preview: 'text', delete: false },
        }),
        row({
          id: `recording:${REC}`,
          source: 'recording',
          kind: 'recording',
          name: 'Voice recording',
          conversation: null,
          media: { status: 'done', duration_ms: 61_000, has_transcript: true },
          can: { download: true, preview: 'audio', delete: true },
        }),
      ],
      next_cursor: 'abc',
      retention: RETENTION,
    });
    expect(page).not.toBeNull();
    expect(page!.nextCursor).toBe('abc');
    expect(page!.retention).toEqual({
      uploadHours: 24,
      filesKeptWithChat: false,
      recordingDays: 0,
      videoKeptWithChat: true,
      videoGraceHours: 72,
      picturesBrowserOnly: true,
      picturesKept: false,
      pictureMemoryHours: 2,
    });
    const [doc, text, rec] = page!.items;
    expect(doc).toMatchObject({
      id: `upload:${HEX}`,
      source: 'upload',
      kind: 'document',
      name: 'Q3 report.pdf',
      bytes: 2_400_000,
      createdAt: '2026-09-30T09:14:00+00:00',
      conversation: { id: 'conv-1', title: 'Quarterly planning' },
      availability: 'available',
      media: null,
      can: { download: true, preview: null, delete: false },
      uploadId: HEX,
      recordingId: null,
    });
    expect(text).toMatchObject({ source: 'text', bytes: null, uploadId: null, availability: 'text_only' });
    expect(rec).toMatchObject({
      kind: 'recording',
      recordingId: REC,
      conversation: null,
      media: { status: 'done', durationMs: 61_000, hasTranscript: true },
    });
  });

  it('promises a transcript only when the server says one has words', () => {
    const recordingRow = (media: Record<string, unknown>) =>
      parsed({
        id: `recording:${REC}`,
        source: 'recording',
        kind: 'recording',
        name: 'Voice recording',
        conversation: null,
        media,
        can: { download: true, preview: 'audio', delete: true },
      });
    expect(recordingRow({ status: 'done', duration_ms: 1, has_transcript: true }).media?.hasTranscript).toBe(true);
    // A done recording that heard no speech (QA 2026-10-01), a flag that is
    // not an explicit true, and an older server that sends none.
    expect(recordingRow({ status: 'done', duration_ms: 1, has_transcript: false }).media?.hasTranscript).toBe(false);
    expect(recordingRow({ status: 'done', duration_ms: 1, has_transcript: 'yes' }).media?.hasTranscript).toBe(false);
    expect(recordingRow({ status: 'done', duration_ms: 1 }).media?.hasTranscript).toBe(false);
  });

  it('drops rows it could not act on, and refuses a body that is not a page', () => {
    const page = parseMyFilesPage({
      items: [
        row(),
        null,
        'upload',
        row({ id: 'upload' }),
        row({ id: 'folder:1' }),
        row({ kind: 'picture' }),
        row({ availability: 'maybe' }),
        row({ name: 42 }),
        row({ created_at: 'yesterday' }),
        row({ source: 'recording', kind: 'recording', id: 'recording:not-hex', conversation: null }),
        row({ conversation: null }), // a chat file with no chat
        row({ can: 'everything' }),
      ],
      next_cursor: null,
    });
    expect(page!.items.map((f) => f.id)).toEqual([`upload:${HEX}`]);
    expect(page!.retention).toBeNull();
    expect(parseMyFilesPage(null)).toBeNull();
    expect(parseMyFilesPage({ items: 'nope' })).toBeNull();
    expect(parseMyFilesPage([])).toBeNull();
  });

  it('never offers a download or delete the server did not grant', () => {
    const file = parsed({ can: { download: 'yes', preview: 'html', delete: 1 } });
    expect(file.can).toEqual({ download: false, preview: null, delete: false });
  });

  it('appends an older page without repeating a row', () => {
    const a = parsed();
    const b = parsed({ id: `upload:${'c'.repeat(32)}`, name: 'other.pdf' });
    expect(appendFiles([a], [a, b]).map((f) => f.name)).toEqual(['Q3 report.pdf', 'other.pdf']);
  });

  it('reads the summary, missing kinds as zero', () => {
    const summary = parseSummary({
      kinds: { document: { count: 3, bytes: 900 }, recording: { count: 2, bytes: 100 } },
      total: { count: 5, bytes: 1000 },
      retention: RETENTION,
    });
    expect(summary!.kinds.document).toEqual({ count: 3, bytes: 900 });
    expect(summary!.kinds.video).toEqual({ count: 0, bytes: 0 });
    expect(summary!.total).toEqual({ count: 5, bytes: 1000 });
    expect(parseSummary({ kinds: [] })).toBeNull();
  });
});

describe('URLs', () => {
  it('encodes every id it puts in a path', () => {
    expect(downloadUrl(parsed({ conversation: { id: 'conv 1', title: 't' } }))).toBe(
      `/api/uploads/conv%201/${HEX}/file`,
    );
    const rec = parsed({
      id: `recording:${REC}`,
      source: 'recording',
      kind: 'recording',
      conversation: null,
      can: { download: true, preview: 'audio', delete: true },
    });
    expect(downloadUrl(rec)).toBe(`/api/audio/sessions/${REC}/audio`);
    expect(downloadUrl(parsed({ can: { download: false, preview: null, delete: false } }))).toBeNull();
    expect(chatUrl('conv 1&x=2')).toBe('/?c=conv%201%26x%3D2');
  });

  it('gives each row a DOM id a selector can use', () => {
    expect(rowDomId(parsed())).toBe(`file-upload-${HEX}`);
    expect(rowDomId(parsed({ id: 'text:42', source: 'text', bytes: null }))).toBe('file-text-42');
  });
});

describe('filters in the page URL', () => {
  const full: Filters = {
    q: 'budget 50%',
    kind: 'dataset',
    from: '2026-09-01',
    to: '2026-09-30',
    size: '10_100mb',
    sort: 'largest',
  };

  it('survive a round trip through the query string', () => {
    const query = filtersToQuery(full);
    expect(filtersFromQuery(new URLSearchParams(query))).toEqual(full);
    expect(filtersToQuery(DEFAULT_FILTERS)).toBe('');
    expect(filtersFromQuery(new URLSearchParams(''))).toEqual(DEFAULT_FILTERS);
  });

  it('fall back to the default for anything they do not recognise', () => {
    const odd = filtersFromQuery(
      new URLSearchParams('kind=pictures&sort=sideways&size=huge&from=2026-13-40&to=soon&q=' + 'x'.repeat(150)),
    );
    expect(odd).toEqual({ ...DEFAULT_FILTERS, q: 'x'.repeat(100) });
  });

  it('say whether anything but the sort is narrowing the list', () => {
    expect(hasActiveFilters(DEFAULT_FILTERS)).toBe(false);
    expect(hasActiveFilters({ ...DEFAULT_FILTERS, sort: 'name' })).toBe(false);
    expect(hasActiveFilters({ ...DEFAULT_FILTERS, q: 'x' })).toBe(true);
    expect(hasActiveFilters({ ...DEFAULT_FILTERS, size: 'under_1mb' })).toBe(true);
  });

  it('turn into the API query: whole local days, half-open sizes, the cursor', () => {
    const url = new URL(listRequestUrl(full, 'CURSOR', 50), 'http://app.test');
    expect(url.pathname).toBe('/api/files/mine');
    const q = url.searchParams;
    expect(q.get('q')).toBe('budget 50%');
    expect(q.get('kind')).toBe('dataset');
    expect(q.get('since')).toBe(new Date(2026, 8, 1).toISOString());
    expect(q.get('until')).toBe(new Date(2026, 9, 1).toISOString());
    expect(q.get('min_bytes')).toBe(String(10 * 1024 * 1024));
    expect(q.get('max_bytes')).toBe(String(100 * 1024 * 1024));
    expect(q.get('sort')).toBe('largest');
    expect(q.get('limit')).toBe('50');
    expect(q.get('cursor')).toBe('CURSOR');

    const summary = new URL(summaryRequestUrl(full), 'http://app.test');
    expect(summary.pathname).toBe('/api/files/mine/summary');
    expect([...summary.searchParams.keys()].sort()).toEqual(['max_bytes', 'min_bytes', 'q', 'since', 'until']);

    const bare = new URL(listRequestUrl(DEFAULT_FILTERS, null, 50), 'http://app.test');
    expect([...bare.searchParams.keys()]).toEqual(['limit']);
  });

  it('reads a calendar day as the local day it names', () => {
    expect(dayStartIso('2026-02-28')).toBe(new Date(2026, 1, 28).toISOString());
    expect(nextDayStartIso('2026-02-28')).toBe(new Date(2026, 2, 1).toISOString());
    expect(dayStartIso('2026-02-30')).toBeNull();
    expect(dayStartIso('')).toBeNull();
  });
});

describe('what each row offers', () => {
  it('previews what a browser can draw from the bytes, up to 25 MB', () => {
    expect(previewPlanFor(parsed())).toEqual({
      kind: 'bytes',
      ref: { conversationId: 'conv-1', uploadId: HEX },
    });
    const big = parsed({ bytes: BYTE_PREVIEW_MAX_BYTES + 1 });
    expect(previewPlanFor(big)).toBeNull();
    const bigWithText = parsed({
      bytes: BYTE_PREVIEW_MAX_BYTES + 1,
      can: { download: true, preview: 'text', delete: false },
    });
    expect(previewPlanFor(bigWithText)).toEqual({ kind: 'text', conversationId: 'conv-1', name: 'Q3 report.pdf' });
  });

  it('reads a document the browser cannot draw from its extracted text', () => {
    const docx = parsed({ name: 'contract.docx', can: { download: true, preview: 'text', delete: false } });
    expect(previewPlanFor(docx)).toEqual({ kind: 'text', conversationId: 'conv-1', name: 'contract.docx' });
    const textOnly = parsed({
      id: 'text:7',
      source: 'text',
      name: 'old.pdf',
      bytes: null,
      availability: 'text_only',
      can: { download: false, preview: 'text', delete: false },
    });
    expect(previewPlanFor(textOnly)).toEqual({ kind: 'text', conversationId: 'conv-1', name: 'old.pdf' });
  });

  it('reads an archive sent as a document from the manifest the chat kept, not from its own name', () => {
    // The composer sends a .zip on the DOCUMENT rail (found by the 2026-09-30
    // end-to-end run). The chat keeps "<name> (archive contents)", never a
    // text row under the archive's own name, and the server names that row.
    const zip = parsed({
      name: 'customer-export.zip',
      bytes: 317,
      can: { download: true, preview: 'text', delete: false },
      text_name: 'customer-export.zip (archive contents)',
    });
    expect(zip.textName).toBe('customer-export.zip (archive contents)');
    expect(previewPlanFor(zip)).toEqual({
      kind: 'text',
      conversationId: 'conv-1',
      name: 'customer-export.zip (archive contents)',
    });
    // No text preview granted: the name the server sent is ignored.
    expect(parsed({ text_name: 'elsewhere.txt' }).textName).toBeNull();
    // Not a string: ignored, and the file's own name is read.
    const odd = parsed({ name: 'notes.docx', can: { download: true, preview: 'text', delete: false }, text_name: 7 });
    expect(previewPlanFor(odd)).toEqual({ kind: 'text', conversationId: 'conv-1', name: 'notes.docx' });
  });

  it('shows a spreadsheet summary after its bytes are gone, and never renders markup files', () => {
    const summary = parsed({
      kind: 'dataset',
      name: 'sales.xlsx',
      availability: 'summary_only',
      can: { download: false, preview: 'summary', delete: false },
    });
    expect(previewPlanFor(summary)).toEqual({ kind: 'summary', conversationId: 'conv-1', uploadId: HEX });
    const csv = parsed({ kind: 'dataset', name: 'sales.csv' });
    expect(previewPlanFor(csv)).toMatchObject({ kind: 'bytes' });
    expect(previewPlanFor(parsed({ name: 'page.html' }))).toBeNull();
    expect(previewPlanFor(parsed({ name: 'logo.svg' }))).toBeNull();
    expect(previewPlanFor(parsed({ availability: 'expired', can: { download: false, preview: null, delete: false } }))).toBeNull();
  });

  it('plays a recording and downloads a video, previewing neither', () => {
    const rec = parsed({
      id: `recording:${REC}`,
      source: 'recording',
      kind: 'recording',
      conversation: null,
      can: { download: true, preview: 'audio', delete: true },
    });
    expect(previewPlanFor(rec)).toEqual({ kind: 'audio', url: `/api/audio/sessions/${REC}/audio` });
    expect(previewPlanFor(parsed({ kind: 'video', name: 'standup.mp4' }))).toBeNull();
  });
});

describe('words', () => {
  it('names kinds and states in plain language', () => {
    expect(KIND_LABEL).toEqual({
      document: 'Document',
      dataset: 'Spreadsheet or data',
      image: 'Picture',
      video: 'Video',
      audio: 'Audio',
      recording: 'Voice recording',
    });
    expect(KIND_FILTER_LABEL.dataset).toBe('Spreadsheets & data');
    expect(AVAILABILITY_LABEL).toEqual({
      available: 'Stored',
      text_only: 'Text only',
      summary_only: 'Summary only',
      processing: 'Processing',
      expired: 'Removed',
    });
  });

  it('builds the retention sentences from the deployment', () => {
    const parsedRetention = parseMyFilesPage({ items: [], next_cursor: null, retention: RETENTION })!.retention!;
    const text = retentionSentences(parsedRetention).join(' ');
    // Not "up to": nothing removes a chat file before its hours are up, the
    // quota included, and nothing removes it on the hour either: the sweep
    // runs when someone next uploads (QA 2026-10-01).
    expect(text).toContain(
      "Files you attach to a chat are kept for 24 hours, then removed the next time the server clears out old files; after that the chat keeps what it read (a document's text, a spreadsheet's summary).",
    );
    expect(text).not.toContain('up to');
    // Deleting a chat is not erasure: the bytes wait for the server's clean-up.
    expect(text).toContain(
      'Deleting a chat takes its files off this list at once; the server erases their stored copies later.',
    );
    expect(text).toContain('Voice recordings stay until you delete them.');
    expect(text).toContain('Pictures stay only in the browser you sent them from.');
    const monthly = parseMyFilesPage({
      items: [],
      next_cursor: null,
      retention: { ...RETENTION, upload_hours: 1, recording_days: 30 },
    })!.retention!;
    const other = retentionSentences(monthly).join(' ');
    expect(other).toContain('kept for 1 hour, then removed the next time the server clears out old files;');
    expect(other).toContain('Voice recordings are deleted automatically 30 days after they finish.');
  });
});

describe('the note under a row that is not simply stored', () => {
  it('says a swept file was removed after its hours, which nothing shortens', () => {
    const retention = parseMyFilesPage({ items: [], next_cursor: null, retention: RETENTION })!.retention!;
    const none = { download: false, preview: null, delete: false };
    expect(availabilityNote(parsed({ availability: 'text_only', can: { ...none, preview: 'text' } }), retention)).toBe(
      'The file was removed after 24 hours. The text the chat read is kept.',
    );
    expect(
      availabilityNote(parsed({ kind: 'dataset', name: 'sales.csv', availability: 'summary_only', can: { ...none, preview: 'summary' } }), retention),
    ).toBe('The file was removed after 24 hours. The summary the chat made of it is kept.');
    expect(availabilityNote(parsed({ availability: 'expired', can: none }), retention)).toBe(
      'The file was removed after 24 hours, and nothing of it was kept.',
    );
    // Without the server's retention block, no number is invented.
    expect(availabilityNote(parsed({ availability: 'expired', can: none }), null)).toBe(
      'The file was removed, and nothing of it was kept.',
    );
    expect(availabilityNote(parsed(), retention)).toBeNull();
  });
});

describe('a spreadsheet or dataset summary from its stored profile', () => {
  it('reads a workbook, a single table and an archive of tables', () => {
    const workbook = summaryFromProfile(
      [{ file: 'sales.xlsx', kind: 'spreadsheet', sheets: [{ name: 'Q3', rows: 2, columns: [{ name: 'a' }], sample_rows: [{ a: 1 }] }] }],
      'sales.xlsx',
    );
    expect(workbook!.sheets.map((s) => s.name)).toEqual(['Q3']);

    const table = summaryFromProfile(
      [{ file: 'sales.csv', kind: 'table', rows: 995, columns: [{ name: 'region' }, { name: 'amount' }], sample_rows: [{ region: 'north', amount: 10 }] }],
      'sales.csv',
    );
    expect(table).toEqual({
      filename: 'sales.csv',
      sheets: [
        {
          name: 'sales.csv',
          rows: 995,
          columns: ['region', 'amount'],
          previewRows: [{ region: 'north', amount: 10 }],
          complete: false,
        },
      ],
    });

    const archive = summaryFromProfile(
      [
        { file: 'a.csv', kind: 'table', rows: 1, columns: [{ name: 'x' }], full_rows: [{ x: 1 }] },
        { file: 'notes.txt', kind: 'other' },
        { file: 'b.csv', kind: 'table', rows: 1, columns: [{ name: 'y' }], full_rows: [{ y: 2 }] },
      ],
      'bundle.zip',
    );
    expect(archive!.sheets.map((s) => [s.name, s.complete])).toEqual([
      ['a.csv', true],
      ['b.csv', true],
    ]);
    expect(summaryFromProfile('garbage', 'x.csv')).toBeNull();
    expect(summaryFromProfile([{ file: 'x.bin', kind: 'other' }], 'x.bin')).toBeNull();
  });
});

/* ---------------------------------------------------------- pictures */

/**
 * 2026-10-02 (docs/chat-media/CONTRACT.md §9): a photo sent in a chat is kept
 * on the server for the life of the chat and listed here as kind `image`.
 * The row shape is the one written down for the backend in
 * docs/chat-media/NOTES.md (fe-files).
 */
describe('stored chat pictures', () => {
  const MEDIA = 'e'.repeat(32);
  const ATT = 'img-leaf-0001';

  function picture(over: Record<string, unknown> = {}): Record<string, unknown> {
    return row({
      id: `media:${MEDIA}`,
      source: 'media',
      kind: 'image',
      name: 'leaf.jpg',
      bytes: 412_000,
      attachment_id: ATT,
      media: { width: 1600, height: 1200, mime: 'image/jpeg' },
      can: { download: true, preview: 'image', delete: false },
      ...over,
    });
  }

  function only(raw: Record<string, unknown>): MyFile | null {
    return parseMyFilesPage({ items: [raw], next_cursor: null, retention: RETENTION })!.items[0] ?? null;
  }

  it('reads a picture row and builds every URL from its chat and attachment id', () => {
    const file = only(picture())!;
    expect(file).toMatchObject({
      id: `media:${MEDIA}`,
      source: 'media',
      kind: 'image',
      name: 'leaf.jpg',
      bytes: 412_000,
      availability: 'available',
      picture: { conversationId: 'conv-1', attachmentId: ATT },
      uploadId: null,
      recordingId: null,
      media: null,
    });
    expect(file.can.preview).toBe('image');
    // Preview opens the full picture; Download points at the same route.
    expect(previewPlanFor(file)).toEqual({ kind: 'picture', ref: { conversationId: 'conv-1', attachmentId: ATT } });
    expect(downloadUrl(file)).toBe(`/api/chat-media/conv-1/${ATT}?size=full`);
    expect(availabilityNote(file, null)).toBeNull();
  });

  it('never takes a URL from the row: an id is read from a thumbnail URL of the SAME chat only', () => {
    const fromUrl = only(
      picture({ attachment_id: undefined, thumb_url: `/api/chat-media/conv-1/${ATT}?size=thumb` }),
    )!;
    expect(fromUrl.picture).toEqual({ conversationId: 'conv-1', attachmentId: ATT });
    expect(downloadUrl(fromUrl)).toBe(`/api/chat-media/conv-1/${ATT}?size=full`);
    expect(
      only(picture({ attachment_id: undefined, thumbnail_url: `/chat-media/conv-1/${ATT}?size=thumb` }))!.picture,
    ).toEqual({ conversationId: 'conv-1', attachmentId: ATT });
    // Another chat's URL, an off-site URL and no id at all: nothing to show.
    expect(only(picture({ attachment_id: undefined, thumb_url: `/api/chat-media/conv-2/${ATT}?size=thumb` }))).toBeNull();
    expect(only(picture({ attachment_id: undefined, thumb_url: 'https://evil.example/x.png' }))).toBeNull();
    expect(only(picture({ attachment_id: undefined }))).toBeNull();
    expect(only(picture({ attachment_id: '../../etc' }))).toBeNull();
    // ...but the id may also ride inside `media`.
    expect(only(picture({ attachment_id: undefined, media: { attachment_id: ATT } }))!.picture?.attachmentId).toBe(ATT);
  });

  it('accepts the spellings the backend might use for the source, and nothing mixed', () => {
    for (const source of ['image', 'chat_media']) {
      const file = only(picture({ id: `${source}:${MEDIA}`, source }))!;
      expect(file.source).toBe('media');
      expect(file.kind).toBe('image');
    }
    // A picture from the upload store, or a document from the picture store.
    expect(only(row({ kind: 'image' }))).toBeNull();
    expect(only(picture({ kind: 'document' }))).toBeNull();
    // The id's prefix still has to name its source.
    expect(only(picture({ id: `upload:${MEDIA}` }))).toBeNull();
    // A reserved-looking or malformed chat id is never put in a URL.
    expect(only(picture({ conversation: { id: 'a/b', title: 'x' } }))).toBeNull();
  });

  it('names a picture the server kept no name for after its type', () => {
    expect(only(picture({ name: null }))!.name).toBe('Picture.jpg');
    expect(only(picture({ name: '  ', media: { mime: 'image/png' }, attachment_id: ATT }))!.name).toBe('Picture.png');
    expect(only(picture({ name: undefined, media: null }))!.name).toBe('Picture');
  });

  it('a picture the server no longer has offers nothing and says so plainly', () => {
    const file = only(picture({ availability: 'expired', can: { download: false, preview: null, delete: false } }))!;
    expect(previewPlanFor(file)).toBeNull();
    expect(downloadUrl(file)).toBeNull();
    expect(availabilityNote(file, null)).toBe('This picture is no longer stored.');
  });

  it('the summary says which kinds the server counted, so a Pictures filter is offered only by a server that has them', () => {
    const old = parseSummary({ kinds: { document: { count: 1, bytes: 9 } }, total: { count: 1, bytes: 9 } })!;
    expect(old.reported).toEqual(['document']);
    expect(old.kinds.image).toEqual({ count: 0, bytes: 0 });
    const now = parseSummary({
      kinds: { document: { count: 1, bytes: 9 }, image: { count: 4, bytes: 1_600_000 } },
      total: { count: 5, bytes: 1_600_009 },
    })!;
    expect(now.reported).toEqual(['document', 'image']);
    expect(now.kinds.image).toEqual({ count: 4, bytes: 1_600_000 });
  });

  it('the kind filter round-trips through the page URL', () => {
    expect(filtersFromQuery(new URLSearchParams('kind=image')).kind).toBe('image');
    expect(listRequestUrl({ ...DEFAULT_FILTERS, kind: 'image' }, null)).toContain('kind=image');
  });

  it('retention: a server that keeps pictures says so, and how an older one arrives', () => {
    const kept = parseMyFilesPage({
      items: [],
      next_cursor: null,
      retention: { ...RETENTION, pictures: 'kept_with_chat' },
    })!.retention!;
    expect(kept.picturesKept).toBe(true);
    expect(kept.picturesBrowserOnly).toBe(false);
    const text = retentionSentences(kept).join(' ');
    expect(text).toContain('Pictures stay while their chat exists.');
    expect(text).toContain('once the browser that sent it opens its chat again');
    expect(text).not.toContain('only in the browser you sent them from');
    // No word at all is not a promise either way.
    const silent = parseMyFilesPage({
      items: [],
      next_cursor: null,
      retention: { ...RETENTION, pictures: undefined },
    })!.retention!;
    expect(silent.picturesKept).toBe(false);
    expect(retentionSentences(silent).join(' ')).not.toContain('Pictures');
  });

  it('retention: a lasting copy gets its own sentence; the workspace sentence and the swept note stay as they are', () => {
    // Precedence (merge of dev 3fead415, its owner's rule): the "kept for N
    // hours, then removed the next time the server clears out old files"
    // sentence and the "removed after N hours" note are never reworded for
    // the lasting copy, which is said in a sentence of its own. None of them
    // promises a deletion time.
    const kept = parseMyFilesPage({
      items: [],
      next_cursor: null,
      retention: { ...RETENTION, files_kept_with_chat: true },
    })!.retention!;
    expect(kept.filesKeptWithChat).toBe(true);
    const sentences = retentionSentences(kept);
    expect(sentences[0]).toBe(
      "Files you attach to a chat are kept for 24 hours, then removed the next time the server clears out old files; after that the chat keeps what it read (a document's text, a spreadsheet's summary).",
    );
    expect(sentences[1]).toBe(
      'Documents and spreadsheets also keep a copy that stays while their chat exists, unless the server was short of space when they were sent.',
    );
    expect(sentences).toContain('Videos and audio files stay while their chat exists.');
    expect(sentences.join(' ')).not.toContain('up to');
    const none = { download: false, preview: null, delete: false };
    expect(availabilityNote(parsed({ availability: 'text_only', can: { ...none, preview: 'text' } }), kept)).toBe(
      'The file was removed after 24 hours. The text the chat read is kept.',
    );
    // Without the flag, no sentence speaks of a lasting copy.
    const plain = parseMyFilesPage({ items: [], next_cursor: null, retention: RETENTION })!.retention!;
    expect(retentionSentences(plain).join(' ')).not.toContain('also keep a copy');
  });
});
