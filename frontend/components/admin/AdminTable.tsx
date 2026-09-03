'use client';

/**
 * The admin list table.
 *
 * FLAT on purpose (2026-09-04). It used to be a rounded bordered card with
 * zebra striping and a filled sticky header — the chat area's card
 * vocabulary applied to a roster, which made forty members read as forty
 * objects stacked in a box. An enterprise settings table is one object: a
 * quiet header rule, hairline separators between rows, and nothing else.
 * The only decoration left is the hover, and it is a whisper.
 *
 * Geometry is deterministic. Columns declare their own width and the table
 * is `table-fixed`, so every row lands on the same tracks and a long email
 * truncates instead of pushing the Role column three pixels right on that
 * one row. That drift is the single most visible difference between a table
 * that was designed and one that was assembled.
 */

import type { ReactNode } from 'react';
import { ErrorPanel, SkeletonLine } from './ui';

export interface AdminColumn<T> {
  key: string;
  label: string;
  align?: 'left' | 'right';
  /**
   * A CSS width for the column's <col>. Given on every column but the one
   * that should absorb the slack (leave that one undefined).
   */
  width?: string;
  /** Hidden below `lg` — for columns that are useful but not essential. */
  hideBelowLg?: boolean;
  render: (row: T) => ReactNode;
}

/**
 * Row height is set here, once: 68px of breathing room around a 40px avatar.
 * `whitespace-nowrap` is load-bearing — without it a two-word cell ("4 hours
 * ago") wraps and that ONE row grows, which is exactly the drift the fixed
 * tracks exist to prevent. Cells that can overflow truncate instead.
 */
const CELL =
  'h-[68px] whitespace-nowrap border-b border-[var(--admin-separator)] px-4 align-middle';

export function AdminTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  loading = false,
  skeletonRows = 5,
  minWidth = 720,
  empty,
  error,
  onRetry,
}: {
  columns: AdminColumn<T>[];
  rows: T[];
  rowKey: (row: T) => string | number;
  onRowClick?: (row: T) => void;
  loading?: boolean;
  skeletonRows?: number;
  /**
   * The width below which the table scrolls sideways instead of squeezing.
   * Set it to the fixed columns PLUS a readable identity column: without
   * that floor, a narrow window steals the slack from the one column that
   * carries the names, and the roster truncates to "Na…" (measured at
   * 1024px before this existed).
   */
  minWidth?: number;
  /** Shown instead of the table when there is nothing to list. */
  empty: string;
  error?: string | null;
  onRetry?: () => void;
}) {
  if (error) {
    return <ErrorPanel message={error} onRetry={onRetry} />;
  }
  if (!loading && rows.length === 0) {
    return (
      <div className="border-t border-[var(--admin-separator)] px-4 py-16 text-center text-sm text-muted">
        {empty}
      </div>
    );
  }
  const hidden = (col: AdminColumn<T>) => (col.hideBelowLg ? 'hidden lg:table-cell' : '');
  // Fixed tracks only when the caller actually declared widths. A table that
  // did not (the audit log, the usage report's eleven columns) keeps auto
  // layout, where the browser's own sizing beats eleven equal thirds.
  const fixed = columns.some((col) => col.width);
  return (
    <div className="-mx-4 overflow-x-auto px-4 md:mx-0 md:px-0">
      <table
        style={{ minWidth }}
        className={`w-full border-collapse text-sm ${
          fixed ? 'table-fixed' : 'table-auto'
        }`}
      >
        <colgroup>
          {columns.map((col) => (
            <col
              key={col.key}
              style={col.width ? { width: col.width } : undefined}
              className={hidden(col)}
            />
          ))}
        </colgroup>
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                scope="col"
                className={`whitespace-nowrap border-b border-border px-4 pb-2.5 text-xs font-semibold text-muted ${
                  col.align === 'right' ? 'text-right' : 'text-left'
                } ${hidden(col)}`}
              >
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading
            ? Array.from({ length: skeletonRows }, (_, i) => (
                <tr key={i}>
                  {columns.map((col, c) => (
                    <td key={col.key} className={`${CELL} ${hidden(col)}`}>
                      <SkeletonLine
                        className={c === 0 ? 'w-40' : c % 2 ? 'w-16' : 'w-24'}
                      />
                    </td>
                  ))}
                </tr>
              ))
            : rows.map((row) => (
                <tr
                  key={rowKey(row)}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={
                    onRowClick
                      ? 'cursor-pointer transition-colors duration-ts hover:bg-[var(--admin-row-hover)]'
                      : 'transition-colors duration-ts hover:bg-[var(--admin-row-hover)]'
                  }
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={`${CELL} ${col.align === 'right' ? 'text-right' : ''} ${hidden(col)}`}
                    >
                      {col.render(row)}
                    </td>
                  ))}
                </tr>
              ))}
        </tbody>
      </table>
    </div>
  );
}

/** Offset pagination footer: "Showing 1–25 of 132" plus Prev / Next. */
export function Pagination({
  total,
  offset,
  limit,
  onOffset,
}: {
  total: number;
  offset: number;
  limit: number;
  onOffset: (offset: number) => void;
}) {
  if (total <= limit && offset === 0) return null;
  const from = Math.min(offset + 1, total);
  const to = Math.min(offset + limit, total);
  const button =
    'rounded-lg border border-border bg-[var(--admin-control)] px-3 py-1.5 text-xs font-medium text-muted transition-colors duration-ts hover:bg-[var(--admin-control-hover)] hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-[var(--admin-control)] disabled:hover:text-muted';
  return (
    <div className="flex items-center justify-between gap-2 pt-4">
      <span className="text-xs text-muted">
        Showing {from.toLocaleString()}–{to.toLocaleString()} of{' '}
        {total.toLocaleString()}
      </span>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          disabled={offset === 0}
          onClick={() => onOffset(Math.max(0, offset - limit))}
          className={button}
        >
          Previous
        </button>
        <button
          type="button"
          disabled={to >= total}
          onClick={() => onOffset(offset + limit)}
          className={button}
        >
          Next
        </button>
      </div>
    </div>
  );
}
