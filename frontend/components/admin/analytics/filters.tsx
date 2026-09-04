'use client';

/**
 * The console's filters, and the URL they live in.
 *
 * Filter state is held in the QUERY STRING, not in component state: a range
 * someone picked survives a reload, comes back with the browser's Back
 * button, and — the reason that matters at work — can be pasted into a
 * message so a colleague opens exactly the view being discussed.
 *
 * One toolbar component so every page's controls share a height (40px), a
 * radius and an order. Pages differ in WHICH filters they show, never in how
 * they look or where they sit.
 */

import { useCallback } from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { AdminSelect } from '../controls';
import { RANGES, type RangeKey } from './types';

const RANGE_KEYS = new Set(RANGES.map((r) => r.key));

/**
 * Read and write one query parameter.
 *
 * `replace` rather than `push` — a filter change is not a navigation, and
 * twenty of them should not bury the page someone arrived from under twenty
 * Back presses. `scroll: false` keeps the reader where they were.
 */
export function useQueryState(
  key: string,
  fallback = '',
): [string, (next: string) => void] {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const value = params.get(key) ?? fallback;
  const set = useCallback(
    (next: string) => {
      const query = new URLSearchParams(params.toString());
      if (!next || next === fallback) query.delete(key);
      else query.set(key, next);
      const search = query.toString();
      router.replace(`${pathname}${search ? `?${search}` : ''}`, {
        scroll: false,
      });
    },
    [key, fallback, params, pathname, router],
  );
  return [value, set];
}

/** The selected window, validated against the closed set the API accepts. */
export function useRange(): [RangeKey, (next: RangeKey) => void] {
  const [raw, set] = useQueryState('range', '30d');
  const value = (RANGE_KEYS.has(raw as RangeKey) ? raw : '30d') as RangeKey;
  return [value, set as (next: RangeKey) => void];
}

export function RangePicker() {
  const [range, setRange] = useRange();
  return (
    <AdminSelect
      value={range}
      onChange={(next) => setRange(next as RangeKey)}
      label="Time range"
      options={RANGES.map((r) => ({ value: r.key, label: r.label }))}
    />
  );
}

/**
 * Narrow the request-shaped figures to one served model.
 *
 * The options come from the response, which lists what has actually served a
 * request on this deployment — a hardcoded model list drifts the first time
 * someone swaps a checkpoint.
 */
export function ModelPicker({ models }: { models: string[] }) {
  const [model, setModel] = useQueryState('model', '');
  if (models.length < 2) return null; // one model is not a choice
  return (
    <AdminSelect
      value={model}
      onChange={setModel}
      label="Model"
      options={[
        { value: '', label: 'All models' },
        ...models.map((m) => ({ value: m, label: m.split('/').pop() ?? m })),
      ]}
    />
  );
}

/**
 * The page header: title on the left, filters on the right.
 *
 * It is a `<header>` with the h1 inside it so the page has exactly one
 * top-level heading, and the filters are grouped so a screen reader announces
 * them as the page's controls rather than as loose selects.
 */
export function ConsoleHeader({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children?: React.ReactNode;
}) {
  return (
    <header className="mb-2 flex flex-wrap items-start justify-between gap-x-4 gap-y-3 pb-4">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {description && (
          <p className="mt-1 max-w-2xl text-sm text-muted">{description}</p>
        )}
      </div>
      {children && (
        <div
          role="group"
          aria-label="Filters"
          className="flex shrink-0 flex-wrap items-center gap-2"
        >
          {children}
        </div>
      )}
    </header>
  );
}
