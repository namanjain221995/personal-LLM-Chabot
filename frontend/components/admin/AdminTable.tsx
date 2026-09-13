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
 *
 * NARROW WINDOWS (responsive audit, 2026-09-13):
 *  · The ROW ACTIONS column (`key: 'actions'`) is pinned to the right edge of
 *    the scroller. A roster that needs 900px in a 720px column (1024px laptop
 *    with the rail open) used to put the ⋯ menu — the only route to Revoke,
 *    Disable or Remove — off-screen with nothing to say it was there. Pinned,
 *    it stays under the finger and the cut-off column beside it is the hint
 *    that the table scrolls.
 *  · Below `lg` the min-width floor drops by the px widths of the columns
 *    that are hidden there, so a phone does not scroll past empty tracks.
 *  · A `<col>` given `lg:table-cell` is not a column to Chrome: it leaves the
 *    colgroup and every later width slides one track left (Last active drew
 *    at the actions column's 56px). The scroller restores `table-column`.
 */

import type { CSSProperties, ReactNode } from 'react';
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

/**
 * The pinned actions cell. Opaque (`bg-bg`) so the columns scrolling beneath
 * it do not show through, with the row's hover tint layered on top as an
 * image so it still reads as one row. Its separator is the same `border-b`
 * every cell has: the table is `border-separate border-spacing-0`, so each
 * cell owns its border and a sticky cell carries its own along. (It was an
 * inset shadow over a collapsed border, and Chrome drew that 1px above the
 * grid line the other cells share — a visible step in every row rule where
 * the pinned column began; re-audit 2026-09-13.)
 *
 * Below md the scroller carries the page's 16px gutter as padding, and a
 * sticky inset is measured inside that padding — at right-0
 * the Role column showed through the 16px strip to the right of the menu —
 * so there it pins 16px further out, flush with the screen edge.
 */
const PINNED =
  'sticky right-0 max-md:-right-4 z-[1] bg-bg group-hover:[background-image:linear-gradient(var(--admin-row-hover),var(--admin-row-hover))]';

/** The row-actions column is pinned; see the header note. */
export const isPinnedColumn = (col: { key: string }) => col.key === 'actions';

/**
 * The table's min-width below `lg`: the desktop floor less every px width
 * that is hidden there. Percent widths cannot be subtracted and are ignored.
 */
export function narrowMinWidth(
  minWidth: number,
  columns: { width?: string; hideBelowLg?: boolean }[],
): number {
  const hidden = columns
    .filter((col) => col.hideBelowLg && col.width && /^\d+(\.\d+)?px$/.test(col.width))
    .reduce((sum, col) => sum + parseFloat(col.width as string), 0);
  return Math.max(0, minWidth - hidden);
}

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
  const pinned = (col: AdminColumn<T>) => (isPinnedColumn(col) ? PINNED : '');
  return (
    <div
      data-testid="admin-table-scroll"
      className="-mx-4 overflow-x-auto px-4 md:mx-0 md:px-0 lg:[&_col]:!table-column"
    >
      <table
        style={
          {
            '--admin-table-min': `${minWidth}px`,
            '--admin-table-min-narrow': `${narrowMinWidth(minWidth, columns)}px`,
          } as CSSProperties
        }
        className={`w-full min-w-[var(--admin-table-min-narrow)] border-separate border-spacing-0 text-sm lg:min-w-[var(--admin-table-min)] ${
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
                } ${hidden(col)} ${isPinnedColumn(col) ? 'sticky right-0 max-md:-right-4 z-[1] bg-bg' : ''}`}
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
                    <td key={col.key} className={`${CELL} ${hidden(col)} ${pinned(col)}`}>
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
                      ? 'group cursor-pointer transition-colors duration-ts hover:bg-[var(--admin-row-hover)]'
                      : 'group transition-colors duration-ts hover:bg-[var(--admin-row-hover)]'
                  }
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={`${CELL} ${col.align === 'right' ? 'text-right' : ''} ${hidden(col)} ${pinned(col)}`}
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
