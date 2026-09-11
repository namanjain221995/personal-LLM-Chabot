'use client';

/**
 * Grid viewer for a tabular file — an xlsx or, since 2026-09-12, a csv.
 *
 * Two routes answer, with two shapes, and this component reads both:
 *  - GET …/grid?file=<file_id> (CONTRACT-2 §2, §11) for any file that has an
 *    id: the sheet NAMES, one sheet's window (`columns`, `rows`), the totals
 *    (`total_rows`, `total_columns`) and whether the window is smaller than
 *    the file. Formulas arrive as text inside the cells.
 *  - GET …/sheets (the older route, kept as an alias) for a workbook ref
 *    persisted before file ids existed: the sheet list with sizes, one
 *    sheet's window, and a `formulas` map by address.
 * Both are folded into one `GridView` so the table below has one shape to
 * draw, and nothing pretends to be what it is not: a formula cell shows the
 * formula with a small ƒ so a reader knows the number comes from Excel, not
 * from us, and a truncated window says so in words rather than ending
 * quietly at row 200 as if the sheet did.
 *
 * Column letters and row numbers are shown because that is how the formula
 * text refers to cells (`=SUM(B2:B11)`); without them the ƒ marker would
 * point at nothing a reader could find. Sheet tabs appear only when there
 * is more than one sheet — a CSV has exactly one, and a tab strip with a
 * single tab is a decoration.
 */

import { useEffect, useState } from 'react';
import {
  ArtifactRequestError,
  fetchGrid,
  fetchSheets,
  isFileId,
  type GridResponse,
  type SheetsResponse,
} from '@/lib/artifacts';
import { stateForStatus, ViewerState, type ViewerStateKind } from './ViewerState';

/** 0 → A, 25 → Z, 26 → AA — Excel's column letters. */
export function columnLetter(index: number): string {
  let n = Math.trunc(index) + 1;
  let out = '';
  while (n > 0) {
    const rem = (n - 1) % 26;
    out = String.fromCharCode(65 + rem) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

/**
 * The address of a data cell. Row 1 of the sheet is the header (`columns`),
 * so data row 0 is Excel row 2 — which is where the server's formula keys
 * point.
 */
export function cellAddress(colIndex: number, rowIndex: number): string {
  return `${columnLetter(colIndex)}${rowIndex + 2}`;
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  if (typeof value === 'boolean') return value ? 'TRUE' : 'FALSE';
  return String(value);
}

/** The one shape the table draws, whichever route answered. */
export interface GridView {
  /** Every sheet's name, with its size when the route said. */
  sheets: { name: string; rows?: number; cols?: number }[];
  /** The shown sheet, or null when the file has no visible sheet at all. */
  sheet: {
    name: string;
    columns: string[];
    rows: unknown[][];
    truncated: boolean;
    /** "B12" → "=SUM(B2:B11)" — shown as text, never evaluated. */
    formulas: Record<string, string>;
  } | null;
  /** The file's real size, when known — what the footer states. */
  totalRows?: number;
  totalColumns?: number;
}

/**
 * The older `/sheets` answer. `sheet` is null for a workbook with no
 * visible sheet (preview.sheet_grid answers `{sheets: [], sheet: None}`),
 * and reading `.rows` off it would take the whole panel down.
 */
export function fromSheets(data: SheetsResponse): GridView {
  const sheets = (data.sheets ?? []).map((s) => ({ name: s.name, rows: s.rows, cols: s.cols }));
  const sheet = data.sheet
    ? {
        name: data.sheet.name,
        columns: data.sheet.columns ?? [],
        rows: data.sheet.rows ?? [],
        truncated: Boolean(data.sheet.truncated),
        formulas: data.sheet.formulas ?? {},
      }
    : null;
  const meta = sheet ? sheets.find((s) => s.name === sheet.name) : undefined;
  return { sheets, sheet, totalRows: meta?.rows, totalColumns: meta?.cols };
}

/** The `/grid` answer (CONTRACT-2 §11): names, one window, totals. */
export function fromGrid(data: GridResponse): GridView {
  const names = Array.isArray(data.sheets) ? data.sheets.map((n) => String(n)) : [];
  const columns = Array.isArray(data.columns) ? data.columns.map((c) => String(c ?? '')) : [];
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const name = typeof data.sheet === 'string' ? data.sheet : (names[0] ?? '');
  const sheet =
    names.length === 0 && columns.length === 0 && rows.length === 0
      ? null
      : { name, columns, rows, truncated: Boolean(data.truncated), formulas: {} };
  return {
    sheets: names.map((n) => ({ name: n })),
    sheet,
    totalRows: typeof data.total_rows === 'number' ? data.total_rows : undefined,
    totalColumns: typeof data.total_columns === 'number' ? data.total_columns : undefined,
  };
}

type State =
  | { kind: 'loading' }
  | { kind: 'ready'; view: GridView }
  | { kind: ViewerStateKind; detail?: string };

/** How many rows one window asks for — the older route's default, kept for both. */
const WINDOW_ROWS = 200;
const WINDOW_COLS = 50;

export function SheetViewer({
  artifactId,
  version,
  fileId = null,
  title,
}: {
  artifactId: string;
  version: number;
  /**
   * The file's id, when the ref carries one: the grid route is per FILE,
   * which is what lets a version's xlsx and each of its CSVs be looked at
   * separately. Without one (a ref from before file ids) the workbook's
   * `/sheets` alias answers.
   */
  fileId?: string | null;
  title: string;
}) {
  const [sheetName, setSheetName] = useState<string | undefined>(undefined);
  const [state, setState] = useState<State>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const byId = isFileId(fileId) ? fileId : null;

  // A different file starts on its first sheet again.
  useEffect(() => {
    setSheetName(undefined);
  }, [artifactId, version, byId]);

  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: 'loading' });
    void (async () => {
      try {
        const view = byId
          ? fromGrid(
              await fetchGrid(
                artifactId,
                version,
                { file: byId, sheet: sheetName, limit: WINDOW_ROWS },
                controller.signal,
              ),
            )
          : fromSheets(
              await fetchSheets(
                artifactId,
                version,
                { sheet: sheetName, rows: WINDOW_ROWS, cols: WINDOW_COLS },
                controller.signal,
              ),
            );
        if (!controller.signal.aborted) setState({ kind: 'ready', view });
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err instanceof ArtifactRequestError) {
          setState({ kind: stateForStatus(err.status), detail: err.detail });
        } else {
          setState({ kind: 'failed' });
        }
      }
    })();
    return () => controller.abort();
  }, [artifactId, version, byId, sheetName, attempt]);

  if (state.kind === 'loading') return <ViewerState kind="loading" message="Loading the sheet…" />;
  if (state.kind !== 'ready') {
    return (
      <ViewerState
        kind={state.kind}
        detail={state.detail}
        onRetry={() => setAttempt((n) => n + 1)}
      />
    );
  }

  const { sheets, sheet, totalRows, totalColumns } = state.view;
  if (!sheet) {
    return (
      <ViewerState
        kind="empty"
        message="This workbook has no visible sheets."
        detail="Download the file to open it in its own application."
      />
    );
  }
  const { formulas, columns, rows } = sheet;
  const hasFormulas = Object.keys(formulas).length > 0;
  const size =
    typeof totalRows === 'number'
      ? `${totalRows} ${totalRows === 1 ? 'row' : 'rows'}${
          typeof totalColumns === 'number'
            ? ` · ${totalColumns} ${totalColumns === 1 ? 'column' : 'columns'}`
            : ''
        }`
      : `${rows.length} ${rows.length === 1 ? 'row' : 'rows'}`;

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="sheet-viewer">
      {sheets.length > 1 && (
        <div
          role="tablist"
          aria-label="Sheets"
          className="flex shrink-0 gap-1 overflow-x-auto border-b border-border bg-surface px-2 py-1.5"
        >
          {sheets.map((s) => {
            const selected = s.name === sheet.name;
            return (
              <button
                key={s.name}
                type="button"
                role="tab"
                aria-selected={selected}
                onClick={() => setSheetName(s.name)}
                title={
                  typeof s.rows === 'number' && typeof s.cols === 'number'
                    ? `${s.name} · ${s.rows} rows × ${s.cols} columns`
                    : s.name
                }
                className={`shrink-0 rounded-md px-2.5 py-1 text-xs font-medium transition-colors duration-ts ${
                  selected
                    ? 'bg-accent/15 text-accent'
                    : 'text-muted hover:bg-surface-2 hover:text-ink'
                }`}
              >
                {s.name}
              </button>
            );
          })}
        </div>
      )}

      <div
        role="tabpanel"
        aria-label={`${sheet.name} of ${title}`}
        className="min-h-0 flex-1 overflow-auto bg-bg"
      >
        <table className="min-w-full border-separate border-spacing-0 text-xs">
          <thead>
            <tr>
              <th
                scope="col"
                className="sticky left-0 top-0 z-20 w-10 border-b border-r border-border bg-surface-2 px-2 py-1 text-center font-mono text-[10px] font-normal text-faint"
                aria-label="Row"
              />
              {columns.map((c, i) => (
                <th
                  key={`${i}-${c}`}
                  scope="col"
                  className="sticky top-0 z-10 max-w-[320px] truncate border-b border-r border-border bg-surface-2 px-2 py-1 text-left font-medium text-ink"
                  title={`${columnLetter(i)} · ${String(c)}`}
                >
                  <span className="mr-1.5 font-mono text-[10px] font-normal text-faint">
                    {columnLetter(i)}
                  </span>
                  {String(c ?? '')}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, r) => (
              <tr key={r} className="odd:bg-bg even:bg-surface">
                <th
                  scope="row"
                  className="sticky left-0 z-10 border-b border-r border-border bg-surface-2 px-2 py-1 text-center font-mono text-[10px] font-normal text-faint"
                >
                  {r + 2}
                </th>
                {columns.map((_, c) => {
                  const address = cellAddress(c, r);
                  const formula = formulas[address];
                  const value = row?.[c];
                  const shown = formula && (value === null || value === undefined || value === '')
                    ? formula
                    : formatCell(value);
                  const numeric = typeof value === 'number';
                  return (
                    <td
                      key={address}
                      className={`max-w-[320px] truncate border-b border-r border-border px-2 py-1 ${
                        numeric ? 'text-right tabular-nums' : 'text-left'
                      } ${formula ? 'text-muted' : 'text-ink'}`}
                      title={formula ? `${address}: ${formula}` : undefined}
                      data-formula={formula ? 'true' : undefined}
                    >
                      {formula && (
                        <span
                          className="mr-1 font-serif italic text-accent"
                          aria-label="formula"
                          title={formula}
                        >
                          ƒ
                        </span>
                      )}
                      {shown}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="p-6 text-center text-xs text-faint">This sheet has no rows.</p>
        )}
      </div>

      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-t border-border bg-surface px-3 py-1.5 text-[11px] text-faint">
        <span data-testid="sheet-size">{size}</span>
        {hasFormulas && (
          <span>
            <span className="font-serif italic text-accent">ƒ</span> formulas shown as written — values
            are computed when the file is opened
          </span>
        )}
        {sheet.truncated && (
          <span role="status" className="ml-auto text-warn" data-testid="sheet-truncated">
            Showing the first {rows.length} rows
            {typeof totalColumns === 'number' && totalColumns > columns.length
              ? ` and ${columns.length} columns`
              : ''}{' '}
            — download the file for the rest.
          </span>
        )}
      </div>
    </div>
  );
}
