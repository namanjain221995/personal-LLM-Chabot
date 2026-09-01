'use client';

/**
 * The admin list table — DataTable's visual vocabulary (rounded-ts bordered
 * wrapper, sticky bg-surface-2 header, odd/even striping, border-border/60
 * cells, em-dash faints) on a declarative-columns frame. DataTable itself
 * stays the in-chat results widget (max-h-80, virtualised, column inference);
 * admin lists need explicit columns, full-height scrolling, row click-through
 * and built-in loading/empty/error states, so they get their own component
 * rather than a mode flag on that one.
 */

import type { ReactNode } from 'react';
import { ErrorPanel, SkeletonLine } from './ui';

export interface AdminColumn<T> {
  key: string;
  label: string;
  align?: 'left' | 'right';
  render: (row: T) => ReactNode;
}

const CELL = 'whitespace-nowrap border-b border-border/60 px-3 py-2';

export function AdminTable<T>({
  columns,
  rows,
  rowKey,
  onRowClick,
  loading = false,
  skeletonRows = 5,
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
      <div className="rounded-ts border border-border bg-surface px-4 py-10 text-center text-sm text-muted">
        {empty}
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-ts border border-border">
      <table className="w-full border-collapse text-sm">
        <thead className="sticky top-0 z-10">
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                className={`whitespace-nowrap border-b border-border bg-surface-2 px-3 py-2 text-xs font-semibold text-muted ${
                  col.align === 'right' ? 'text-right' : 'text-left'
                }`}
              >
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading
            ? Array.from({ length: skeletonRows }, (_, i) => (
                <tr key={i} className="odd:bg-surface even:bg-surface-2/40">
                  {columns.map((col, c) => (
                    <td key={col.key} className={CELL}>
                      <SkeletonLine
                        className={c === 0 ? 'w-36' : c % 2 ? 'w-16' : 'w-24'}
                      />
                    </td>
                  ))}
                </tr>
              ))
            : rows.map((row) => (
                <tr
                  key={rowKey(row)}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={`odd:bg-surface even:bg-surface-2/40 ${
                    onRowClick
                      ? 'cursor-pointer transition-colors duration-ts hover:bg-surface-2'
                      : ''
                  }`}
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={`${CELL} ${col.align === 'right' ? 'text-right' : ''}`}
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
    'rounded-md border border-border bg-surface px-2.5 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-surface disabled:hover:text-muted';
  return (
    <div className="mt-3 flex items-center justify-between gap-2">
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
