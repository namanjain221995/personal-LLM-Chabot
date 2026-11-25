'use client';

/**
 * The HTML legend of a chat chart (charts design v2 §8.2).
 *
 * Real buttons outside the canvas: they wrap like text, never sit on axis
 * labels, carry the full name (`dir="auto"`, breaks anywhere rather than
 * truncating), and toggle a series or part. `aria-pressed` is "shown".
 * Values and shares (pie parts) are part of the item, so a pie needs no slice
 * labels on the canvas.
 */

import { useRef } from 'react';

export interface LegendItem {
  id: string;
  label: string;
  color: string;
  /** Formatted value (pie parts). */
  value?: string;
  /** Formatted share (pie parts). */
  share?: string;
}

export function ChartLegend({
  items,
  hidden,
  onToggle,
  onShowAll,
  onHover,
  layout = 'row',
  label = 'Legend',
}: {
  items: LegendItem[];
  hidden: ReadonlySet<string>;
  onToggle: (id: string) => void;
  onShowAll: () => void;
  onHover?: (id: string | null) => void;
  layout?: 'row' | 'column';
  label?: string;
}) {
  const allHidden = items.length > 0 && items.every((i) => hidden.has(i.id));
  const listRef = useRef<HTMLUListElement | null>(null);
  return (
    <div className="chart-legend min-w-0" role="group" aria-label={label}>
      <ul
        ref={listRef}
        className={
          layout === 'column'
            ? 'm-0 flex list-none flex-col gap-0.5 p-0'
            : 'm-0 flex list-none flex-wrap gap-x-1 gap-y-0.5 p-0'
        }
      >
        {items.map((item) => {
          const shown = !hidden.has(item.id);
          return (
            <li key={item.id} className="min-w-0 max-w-full">
              <button
                type="button"
                aria-pressed={shown}
                className="chart-legend-item"
                onClick={() => onToggle(item.id)}
                onMouseEnter={() => onHover?.(item.id)}
                onMouseLeave={() => onHover?.(null)}
                onFocus={() => onHover?.(item.id)}
                onBlur={() => onHover?.(null)}
              >
                <span
                  aria-hidden
                  className="chart-swatch"
                  style={{ color: item.color, background: item.color }}
                />
                <span className="min-w-0">
                  <span dir="auto" className="chart-legend-name break-words [overflow-wrap:anywhere]">
                    {item.label}
                  </span>
                  {item.value !== undefined && (
                    <span className="chart-legend-value ml-1.5 whitespace-nowrap tabular-nums text-muted">
                      {item.value}
                      {item.share ? ` · ${item.share}` : ''}
                    </span>
                  )}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {allHidden && (
        <p className="mt-1 text-xs text-muted">
          All series hidden.{' '}
          <button
            type="button"
            className="chart-inline-btn underline underline-offset-2 hover:text-ink"
            onClick={() => {
              onShowAll();
              // Review A2: this button disappears once used; hand focus to the
              // first legend item instead of letting it fall to the page.
              listRef.current?.querySelector<HTMLButtonElement>('button')?.focus();
            }}
          >
            Show all
          </button>
        </p>
      )}
    </div>
  );
}
