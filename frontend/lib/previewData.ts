/**
 * PHASE 4C — turning an attachment into something a dialog can draw.
 *
 * Three formats, three sources, and NO new dependency in any of them:
 *
 *   CSV/TSV — parsed here. Delimited text with quoting rules is a small,
 *     well-specified problem, and a bounded parser for it is shorter than the
 *     argument for adding a package. It runs over the first slice of the blob,
 *     never the whole file.
 *   XLSX    — read from the PROFILE the orchestrator already stored when the
 *     file was uploaded (sheet names, row counts, columns, sample rows, and
 *     the complete rows for small files). A workbook parser in the browser
 *     would re-derive, from a 200 MB download, what the server computed once.
 *   DOCX    — read from the text engines/document.py already extracted with
 *     the standard library. Never parsed here, never rendered as HTML.
 *
 * Everything below is data. Nothing here renders, and nothing here produces
 * markup — the dialog puts these values in React text nodes, which is what
 * keeps an uploaded file's contents from ever becoming DOM.
 */

/* ------------------------------------------------------------------ CSV */

/** Caps. A dataset may be 200 MB; a dialog needs one screen of it. */
export const MAX_TABLE_ROWS = 200;
export const MAX_TABLE_COLUMNS = 40;

export interface TablePreview {
  columns: string[];
  rows: string[][];
  /** Rows actually parsed from the slice we read. */
  shownRows: number;
  /** True when the file continued past what we read or drew. */
  truncatedRows: boolean;
  truncatedColumns: boolean;
}

/**
 * Split delimited text into rows of cells.
 *
 * RFC 4180 quoting, because half-parsing it is worse than not parsing it: a
 * quoted field may contain the delimiter, a newline, and a doubled quote, and a
 * naive `split(',')` turns any address column into visible nonsense.
 *
 * `limit` stops the walk early — this is a preview, and a 200 MB file must not
 * be turned into 200 MB of arrays to show 200 rows of it.
 */
export function parseDelimited(
  text: string,
  delimiter: string,
  /** Read ONE row past what will be drawn, so "there is more" is knowable. */
  limit: number = MAX_TABLE_ROWS + 2,
): string[][] {
  const rows: string[][] = [];
  let cell = '';
  let row: string[] = [];
  let quoted = false;

  const endCell = () => {
    row.push(cell);
    cell = '';
  };
  const endRow = () => {
    endCell();
    // A trailing newline yields one empty cell, which is not a row.
    if (row.length > 1 || row[0] !== '') rows.push(row);
    row = [];
  };

  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch !== '"') {
        cell += ch;
      } else if (text[i + 1] === '"') {
        cell += '"';
        i += 1; // an escaped quote, not the end of the field
      } else {
        quoted = false;
      }
      continue;
    }
    if (ch === '"' && cell === '') {
      quoted = true;
    } else if (ch === delimiter) {
      endCell();
    } else if (ch === '\n') {
      endRow();
      if (rows.length >= limit) return rows;
    } else if (ch !== '\r') {
      cell += ch;
    }
  }
  if (cell !== '' || row.length > 0) endRow();
  return rows;
}

/** The delimiter a file's NAME implies. Extension-first, like everything else. */
export function delimiterFor(name: string): string {
  return /\.tsv$/i.test(name.trim()) ? '\t' : ',';
}

/**
 * A table from delimited text, bounded in both directions.
 *
 * The first row is the header, which is what every CSV this app accepts has —
 * and when it does not, showing the first data row as headings is a smaller
 * lie than inventing "Column 1..N" over data that may be headerless anyway.
 */
export function tableFromDelimited(
  text: string,
  name: string,
  /** True when the text handed in was already cut short of the whole file. */
  sourceTruncated = false,
): TablePreview | null {
  const parsed = parseDelimited(text, delimiterFor(name));
  if (parsed.length === 0) return null;

  const header = parsed[0];
  const truncatedColumns = header.length > MAX_TABLE_COLUMNS;
  const columns = header.slice(0, MAX_TABLE_COLUMNS);
  const body = parsed.slice(1, MAX_TABLE_ROWS + 1);
  return {
    columns,
    rows: body.map((r) => {
      const cells = r.slice(0, columns.length);
      // Ragged rows are normal in exported data; pad rather than drop them.
      while (cells.length < columns.length) cells.push('');
      return cells;
    }),
    shownRows: body.length,
    truncatedRows: sourceTruncated || parsed.length > MAX_TABLE_ROWS + 1,
    truncatedColumns,
  };
}

/* --------------------------------------------------------------- XLSX */

/** One sheet, as the orchestrator's profiler stores it. */
export interface SheetProfile {
  name: string;
  /** Rows in the REAL sheet — not the number we were given. */
  rows: number;
  columns: string[];
  /** What we can actually show: the complete rows when the file was small
      enough to ship in full, otherwise the profiler's sample. */
  previewRows: Array<Record<string, unknown>>;
  /** True when `previewRows` IS the sheet, not a sample of it. */
  complete: boolean;
}

export interface WorkbookPreview {
  filename: string;
  sheets: SheetProfile[];
}

const asRecord = (v: unknown): Record<string, unknown> =>
  v && typeof v === 'object' && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : {};

/**
 * Read a stored upload profile into something a workbook view can draw.
 *
 * Shapes are checked rather than assumed at every level: this JSON is produced
 * from a file a user uploaded, so its column names and cell values are their
 * text, and a malformed profile must render nothing rather than throw inside a
 * dialog.
 */
export function workbookFromProfile(
  profile: unknown,
  filename: string,
): WorkbookPreview | null {
  const files = Array.isArray(profile) ? profile : [];
  const entry = files
    .map(asRecord)
    .find(
      (f) =>
        f.kind === 'spreadsheet' &&
        (f.file === filename || files.length === 1),
    );
  if (!entry) return null;

  const sheets = (Array.isArray(entry.sheets) ? entry.sheets : [])
    .map(asRecord)
    .map((s): SheetProfile => {
      const columns = (Array.isArray(s.columns) ? s.columns : [])
        .map((c) => {
          const rec = asRecord(c);
          return typeof rec.name === 'string' ? rec.name : String(c ?? '');
        })
        .slice(0, MAX_TABLE_COLUMNS);
      // `full_rows` is the WHOLE sheet (the profiler ships it for small files);
      // `sample_rows` is a handful. Which one we got decides what the dialog is
      // allowed to claim, so the distinction is carried, not flattened.
      const full = Array.isArray(s.full_rows) ? s.full_rows : null;
      const sample = Array.isArray(s.sample_rows) ? s.sample_rows : [];
      const source = full ?? sample;
      return {
        name: typeof s.name === 'string' ? s.name : 'Sheet',
        rows: typeof s.rows === 'number' ? s.rows : source.length,
        columns,
        previewRows: source.slice(0, MAX_TABLE_ROWS).map(asRecord),
        complete: Boolean(full),
      };
    })
    .filter((s) => s.columns.length > 0 || s.previewRows.length > 0);

  return sheets.length ? { filename, sheets } : null;
}

/** Cells arrive as whatever JSON held. Render them as text, never as objects. */
export function cellText(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

/* --------------------------------------------------------------- DOCX */

export interface DocumentText {
  text: string;
  truncated: boolean;
}

/**
 * Fetch a document's extracted text.
 *
 * Returns null for every failure — a preview that cannot load is a sentence in
 * a dialog, never an error boundary, and the caller has one honest fallback
 * for "no text available" regardless of why.
 */
export async function fetchDocumentText(
  conversationId: string,
  filename: string,
  signal?: AbortSignal,
): Promise<DocumentText | null> {
  try {
    const res = await fetch(
      `/api/uploads/${encodeURIComponent(conversationId)}/document?name=${encodeURIComponent(filename)}`,
      { cache: 'no-store', signal },
    );
    if (!res.ok) return null;
    const body = (await res.json()) as { text?: unknown; truncated?: unknown };
    if (typeof body.text !== 'string' || !body.text) return null;
    return { text: body.text, truncated: body.truncated === true };
  } catch {
    return null;
  }
}

/**
 * Fetch the stored profile for one upload of a conversation.
 *
 * Reuses the existing owner-checked listing rather than adding a second read
 * path to the same data: the endpoint already answers 404 for a conversation
 * that is not yours, and already reports a swept upload as `expired`.
 */
export async function fetchUploadProfile(
  conversationId: string,
  uploadId: string,
  signal?: AbortSignal,
): Promise<{ profile: unknown; filename: string; expired: boolean } | null> {
  try {
    const res = await fetch(
      `/api/uploads/${encodeURIComponent(conversationId)}`,
      { cache: 'no-store', signal },
    );
    if (!res.ok) return null;
    const body = (await res.json()) as { uploads?: unknown };
    const rows = Array.isArray(body.uploads) ? body.uploads.map(asRecord) : [];
    const row = rows.find((r) => r.id === uploadId);
    if (!row) return null;
    // `expired` is reported but does NOT withhold the profile. The TTL sweeps
    // the BYTES; the profile is a database row and is still there, so a
    // workbook stays previewable long after its file is gone.
    return {
      profile: row.profile,
      filename: typeof row.filename === 'string' ? row.filename : '',
      expired: row.status === 'expired',
    };
  } catch {
    return null;
  }
}
