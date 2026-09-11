'use client';

/**
 * Grid viewer for a workbook version (`preview_kind: "grid"`).
 *
 * The server answers GET .../sheets with the sheet list and ONE sheet's
 * bounded window — up to 200 rows × 50 columns by default — with values as
 * stored and formulas as TEXT (docs/artifact-studio/API.md). Nothing is
 * evaluated here, and nothing pretends to be: a formula cell shows the
 * formula with a small ƒ so a reader knows the number comes from Excel,
 * not from us, and a truncated window says so in words rather than ending
 * quietly at row 200 as if the sheet did.
 *
 * Column letters and row numbers are shown because that is how the formula
 * text refers to cells (`=SUM(B2:B11)`); without them the ƒ marker would
 * point at nothing a reader could find.
 */

import { useEffect, useState } from 'react';
import { ArtifactRequestError, fetchSheets, type SheetsResponse } from '@/lib/artifacts';
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

type State =
  | { kind: 'loading' }
  | { kind: 'ready'; data: SheetsResponse }
  | { kind: ViewerStateKind; detail?: string };

export function SheetViewer({
  artifactId,
  version,
  title,
}: {
  artifactId: string;
  version: number;
  title: string;
}) {
  const [sheetName, setSheetName] = useState<string | undefined>(undefined);
  const [state, setState] = useState<State>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);

  // A different artifact starts on its first sheet again.
  useEffect(() => {
    setSheetName(undefined);
  }, [artifactId, version]);

  useEffect(() => {
    const controller = new AbortController();
    setState({ kind: 'loading' });
    void (async () => {
      try {
        const data = await fetchSheets(
          artifactId,
          version,
          { sheet: sheetName, rows: 200, cols: 50 },
          controller.signal,
        );
        if (!controller.signal.aborted) setState({ kind: 'ready', data });
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
  }, [artifactId, version, sheetName, attempt]);

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

  const { sheets, sheet } = state.data;
  // A workbook can be valid and still have nothing to show (every sheet
  // hidden, or none written): the server answers `sheet: null`, and reading
  // `.rows` off it would take the whole panel down with a TypeError.
  if (!sheet) {
    return (
      <ViewerState
        kind="empty"
        message="This workbook has no visible sheets."
        detail="Download the file to open it in its own application."
      />
    );
  }
  const formulas = sheet.formulas ?? {};
  const columns = sheet.columns ?? [];
  const rows = sheet.rows ?? [];
  const meta = sheets.find((s) => s.name === sheet.name);

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="sheet-viewer">
      {sheets.length > 0 && (
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
                title={`${s.name} · ${s.rows} rows × ${s.cols} columns`}
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

      <div className="flex shrink-0 items-center gap-3 border-t border-border bg-surface px-3 py-1.5 text-[11px] text-faint">
        <span>
          {meta ? `${meta.rows} rows × ${meta.cols} columns` : `${rows.length} rows`}
        </span>
        {Object.keys(formulas).length > 0 && (
          <span>
            <span className="font-serif italic text-accent">ƒ</span> formulas shown as written — values
            are computed when the file is opened
          </span>
        )}
        {sheet.truncated && (
          <span role="status" className="ml-auto text-warn" data-testid="sheet-truncated">
            Showing the first {rows.length} rows
            {meta && meta.cols > columns.length ? ` and ${columns.length} columns` : ''} — download
            the file for the rest.
          </span>
        )}
      </div>
    </div>
  );
}
