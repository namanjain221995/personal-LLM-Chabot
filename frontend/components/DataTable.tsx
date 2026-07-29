'use client';

/**
 * Proof-drawer Data section (§9): sortable table, client-side CSV download,
 * "first 500 rows" note when the orchestrator truncated the result.
 */

import { useMemo, useState } from 'react';
import type { DataRow } from '@/lib/types';
import { downloadCsv } from '@/lib/csv';
import { IconDownload } from './icons';

type SortDir = 'asc' | 'desc';

function compareValues(a: unknown, b: unknown): number {
  if (a === null || a === undefined) return b === null || b === undefined ? 0 : -1;
  if (b === null || b === undefined) return 1;
  const na = typeof a === 'number' ? a : Number(a);
  const nb = typeof b === 'number' ? b : Number(b);
  if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
  return String(a).localeCompare(String(b));
}

export function DataTable({
  rows,
  truncated,
  csvName,
}: {
  rows: DataRow[];
  truncated?: boolean;
  csvName: string;
}) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>('asc');

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

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-muted">
          {truncated
            ? `Showing the first ${rows.length} rows — the full result was larger.`
            : `${rows.length} row${rows.length === 1 ? '' : 's'}`}
        </span>
        <button
          type="button"
          onClick={() => downloadCsv(rows, csvName)}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
        >
          <IconDownload size={13} />
          Download CSV
        </button>
      </div>
      <div className="max-h-80 overflow-auto rounded-ts border border-border">
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
          <tbody>
            {sorted.map((row, i) => (
              <tr key={i} className="odd:bg-surface even:bg-surface-2/40">
                {columns.map((col) => {
                  const v = row[col];
                  const isNum =
                    typeof v === 'number' ||
                    (v !== '' && v !== null && !Number.isNaN(Number(v)));
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
          </tbody>
        </table>
      </div>
    </div>
  );
}
