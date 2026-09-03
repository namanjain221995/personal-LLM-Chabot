'use client';

/**
 * Proof-drawer Data section (§9): sortable table, client-side CSV download,
 * and a truncation note when the orchestrator could not return everything.
 *
 * WHY THIS IS VIRTUALISED (2026-08-29). The table is meant to show every
 * record the query matched — a Salesforce answer is routinely hundreds of rows
 * and can be tens of thousands. Rendering one <tr> per row put the whole result
 * in the DOM: 28,230 rows x 10 columns is 282,300 cells, which locks the tab.
 * The rows are all present in memory (sorting and CSV still cover the full set);
 * only the slice inside the scroll viewport is mounted, with spacer rows above
 * and below holding the scrollbar at its true size.
 *
 * Below VIRTUALIZE_FROM rows nothing is windowed, so ordinary small results
 * render exactly as they always did.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import type { DataRow } from '@/lib/types';
import { isNumeric, toNumber, type Cell } from '@/lib/chartFormat';
import { downloadCsv } from '@/lib/csv';
import { IconDownload } from './icons';

type SortDir = 'asc' | 'desc';

/** Row count above which only the visible window is mounted. */
const VIRTUALIZE_FROM = 150;
/** Rows rendered beyond each edge of the viewport, so scrolling stays smooth. */
const OVERSCAN = 10;
/** Used until a real row has been measured. */
const ESTIMATED_ROW_HEIGHT = 33;

/**
 * Sorting and cell styling MUST agree on what a number is, so both go through
 * lib/chartFormat — the app's one numeric predicate. It used to be
 * `Number(value)` here and in the cell below, and `Number(true)` is 1, so a
 * checkbox column sorted (and rendered) as if it were a measure.
 */
function compareValues(a: unknown, b: unknown): number {
  if (a === null || a === undefined) return b === null || b === undefined ? 0 : -1;
  if (b === null || b === undefined) return 1;
  const na = toNumber(a as Cell);
  const nb = toNumber(b as Cell);
  if (na !== null && nb !== null) return na - nb;
  return String(a).localeCompare(String(b));
}

export function DataTable({
  rows,
  truncated,
  csvName,
  totalRows,
  fullCsvHref,
  fullCsvRows,
}: {
  rows: DataRow[];
  truncated?: boolean;
  csvName: string;
  /** How many records matched, when more matched than were returned. */
  totalRows?: number;
  /**
   * The COMPLETE result, written server-side and served from /api/reports.
   *
   * `rows` is the preview, capped so the payload and the DOM stay sane. The
   * download button used to serialise that preview, which meant a table
   * captioned "10,000 of 10,423 matching" handed over a 10,000-row file
   * (owner report 2026-08-31). When the orchestrator has written the full
   * export, the button links to it instead of rebuilding the short version
   * in the browser.
   */
  fullCsvHref?: string;
  /** Row count inside that file, for the button's tooltip. */
  fullCsvRows?: number;
}) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const bodyRef = useRef<HTMLTableSectionElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(320);
  const [rowHeight, setRowHeight] = useState(ESTIMATED_ROW_HEIGHT);

  const columns = useMemo(
    () =>
      Array.from(
        rows.reduce<Set<string>>((set, row) => {
          Object.keys(row).forEach((k) => set.add(k));
          return set;
        }, new Set()),
      ),
    [rows],
  );

  const sorted = useMemo(() => {
    if (!sortKey) return rows;
    const out = [...rows].sort((a, b) => compareValues(a[sortKey], b[sortKey]));
    return sortDir === 'desc' ? out.reverse() : out;
  }, [rows, sortKey, sortDir]);

  const virtualised = sorted.length > VIRTUALIZE_FROM;

  // Measure the real geometry once rows exist: an estimated row height that is
  // wrong by a few pixels makes the scrollbar drift over thousands of rows.
  useEffect(() => {
    const scroller = scrollRef.current;
    if (scroller && scroller.clientHeight > 0) setViewport(scroller.clientHeight);
    const first = bodyRef.current?.querySelector<HTMLElement>('tr[data-row]');
    if (first && first.offsetHeight > 0) setRowHeight(first.offsetHeight);
  }, [rows, columns.length, virtualised]);

  const first = virtualised
    ? Math.max(0, Math.floor(scrollTop / rowHeight) - OVERSCAN)
    : 0;
  const last = virtualised
    ? Math.min(sorted.length, Math.ceil((scrollTop + viewport) / rowHeight) + OVERSCAN)
    : sorted.length;
  const visible = virtualised ? sorted.slice(first, last) : sorted;
  const padTop = first * rowHeight;
  const padBottom = Math.max(0, (sorted.length - last) * rowHeight);

  function toggleSort(key: string) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }

  if (rows.length === 0) {
    return <p className="text-sm text-muted">The query returned no rows.</p>;
  }

  const shown = `${rows.length.toLocaleString()} row${rows.length === 1 ? '' : 's'}`;
  // "Narrow the question" was the only way to reach the rest until the full
  // export existed. Where the download now holds everything, say that instead
  // of sending the user back to rewrite a question they answered correctly.
  const rest = fullCsvHref ? 'download for the rest.' : 'narrow the question to see the rest.';
  const label = truncated
    ? totalRows && totalRows > rows.length
      ? `${shown} of ${totalRows.toLocaleString()} matching — ${rest}`
      : `${shown} — the full result was larger; ${rest}`
    : shown;

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-muted">{label}</span>
        {fullCsvHref ? (
          <a
            href={fullCsvHref}
            download={csvName}
            title={
              fullCsvRows
                ? `All ${fullCsvRows.toLocaleString()} rows`
                : 'The complete result'
            }
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs text-muted no-underline transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            <IconDownload size={13} />
            Download CSV{fullCsvRows ? ` (${fullCsvRows.toLocaleString()} rows)` : ''}
          </a>
        ) : (
          <button
            type="button"
            onClick={() => downloadCsv(rows, csvName)}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
          >
            <IconDownload size={13} />
            Download CSV
          </button>
        )}
      </div>
      <div
        ref={scrollRef}
        onScroll={(e) => {
          if (virtualised) setScrollTop(e.currentTarget.scrollTop);
        }}
        className="max-h-80 overflow-auto rounded-ts border border-border"
      >
        <table className="w-full border-collapse text-sm">
          <thead className="sticky top-0 z-10">
            <tr>
              {columns.map((col) => {
                const active = sortKey === col;
                return (
                  <th
                    key={col}
                    aria-sort={
                      active
                        ? sortDir === 'asc'
                          ? 'ascending'
                          : 'descending'
                        : 'none'
                    }
                    className="whitespace-nowrap border-b border-border bg-surface-2 p-0 text-left"
                  >
                    <button
                      type="button"
                      onClick={() => toggleSort(col)}
                      aria-label={`Sort by ${col}`}
                      className="flex w-full items-center gap-1 px-3 py-2 text-xs font-semibold text-muted transition-colors duration-ts hover:text-ink"
                    >
                      {col}
                      <span aria-hidden className="text-faint">
                        {active ? (sortDir === 'asc' ? '↑' : '↓') : ''}
                      </span>
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody ref={bodyRef}>
            {padTop > 0 && (
              <tr aria-hidden style={{ height: padTop }}>
                <td colSpan={columns.length} className="p-0" />
              </tr>
            )}
            {visible.map((row, i) => (
              <tr
                key={first + i}
                data-row
                className="odd:bg-surface even:bg-surface-2/40"
              >
                {columns.map((col) => {
                  const v = row[col];
                  // Booleans are NOT measures. See compareValues above.
                  const isNum = isNumeric(v as Cell);
                  return (
                    <td
                      key={col}
                      className={`whitespace-nowrap border-b border-border/60 px-3 py-1.5 ${
                        isNum ? 'text-right font-mono text-xs' : ''
                      }`}
                    >
                      {v === null || v === undefined ? (
                        <span className="text-faint">—</span>
                      ) : (
                        String(v)
                      )}
                    </td>
                  );
                })}
              </tr>
            ))}
            {padBottom > 0 && (
              <tr aria-hidden style={{ height: padBottom }}>
                <td colSpan={columns.length} className="p-0" />
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
