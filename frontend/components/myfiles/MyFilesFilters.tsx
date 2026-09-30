'use client';

/**
 * The "My files" filter bar: a search box, the type choice with a count for
 * each type, a date range, a size range and the sort order.
 *
 * Every control has a visible label, and the type choice is a real radio
 * group (a fieldset and its legend), so a screen reader hears "Type, Documents
 * 3, radio button, 2 of 6" rather than a row of unlabeled pills. Below 640 px
 * everything but the search box folds behind a Filters button that says
 * whether it is open; nothing here scrolls sideways.
 */

import { useId, type FormEvent } from 'react';
import { IconSearch } from '@/components/icons';
import {
  FILE_KINDS,
  KIND_FILTER_LABEL,
  SEARCH_MAX_CHARS,
  SIZE_BUCKETS,
  SIZE_LABEL,
  SORTS,
  SORT_LABEL,
  hasActiveFilters,
  type FileKind,
  type FileSort,
  type Filters,
  type MyFilesSummary,
  type SizeBucket,
} from '@/lib/myfiles';
import { myFilesActionClass } from './MyFileRow';

const FIELD =
  'mt-1 block h-11 w-full min-w-0 rounded-lg border border-border bg-bg px-3 text-sm text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent';
const LABEL = 'block text-xs font-medium text-muted';

interface MyFilesFiltersProps {
  filters: Filters;
  /** What is in the search box right now (the URL catches up after a pause). */
  draft: string;
  onDraft: (value: string) => void;
  /** Enter in the search box: search now rather than after the pause. */
  onSearchNow: () => void;
  onChange: (next: Partial<Filters>) => void;
  onClear: () => void;
  summary: MyFilesSummary | null;
  open: boolean;
  onToggle: () => void;
}

function Count({ value }: { value: number | undefined }) {
  if (value === undefined) return null;
  return (
    <span className="rounded-full bg-surface-2 px-1.5 text-xs tabular-nums text-muted">
      <span className="sr-only">, </span>
      {value}
    </span>
  );
}

export function MyFilesFilters({
  filters,
  draft,
  onDraft,
  onSearchNow,
  onChange,
  onClear,
  summary,
  open,
  onToggle,
}: MyFilesFiltersProps) {
  const id = useId();
  const panelId = `${id}-filters`;
  const active = hasActiveFilters(filters);

  function submit(e: FormEvent) {
    e.preventDefault();
    onSearchNow();
  }

  function setFrom(value: string) {
    // "Between these two days" either way round: a range typed backwards is
    // turned round rather than refused.
    if (value && filters.to && value > filters.to) onChange({ from: filters.to, to: value });
    else onChange({ from: value });
  }

  function setTo(value: string) {
    if (value && filters.from && value < filters.from) onChange({ from: value, to: filters.from });
    else onChange({ to: value });
  }

  const choices: Array<{ value: FileKind | null; label: string; count: number | undefined }> = [
    { value: null, label: 'All', count: summary?.total.count },
    ...FILE_KINDS.map((kind) => ({ value: kind, label: KIND_FILTER_LABEL[kind], count: summary?.kinds[kind].count })),
  ];

  return (
    <form
      role="search"
      aria-label="Search and filter your files"
      onSubmit={submit}
      className="rounded-ts border border-border bg-surface px-4 py-4 sm:px-5"
    >
      <div className="flex items-end gap-2">
        <div className="min-w-0 flex-1">
          <label htmlFor={`${id}-q`} className={LABEL}>
            Search files and chats
          </label>
          <div className="relative mt-1">
            <IconSearch
              size={16}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
            />
            <input
              id={`${id}-q`}
              type="search"
              value={draft}
              maxLength={SEARCH_MAX_CHARS}
              onChange={(e) => onDraft(e.target.value)}
              placeholder="A file name or a chat title"
              autoComplete="off"
              className="block h-11 w-full min-w-0 rounded-lg border border-border bg-bg pl-9 pr-3 text-sm text-ink placeholder:text-faint focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            />
          </div>
        </div>
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          aria-controls={panelId}
          className={`${myFilesActionClass} shrink-0 sm:hidden`}
        >
          Filters
        </button>
      </div>

      <div id={panelId} className={`${open ? 'block' : 'hidden'} sm:block`}>
        <fieldset className="mt-4 min-w-0">
          <legend className={LABEL}>Type</legend>
          <div className="mt-2 flex flex-wrap gap-2">
            {choices.map((choice) => (
              <label key={choice.value ?? 'all'} className="cursor-pointer">
                <input
                  type="radio"
                  name={`${id}-kind`}
                  value={choice.value ?? ''}
                  checked={filters.kind === choice.value}
                  onChange={() => onChange({ kind: choice.value })}
                  className="peer sr-only"
                />
                <span className="inline-flex min-h-11 items-center gap-2 rounded-full border border-border bg-bg px-3.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink peer-checked:border-accent/60 peer-checked:bg-accent/10 peer-checked:text-accent peer-focus-visible:ring-2 peer-focus-visible:ring-accent">
                  {choice.label}
                  <Count value={choice.count} />
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div className="min-w-0">
            <label htmlFor={`${id}-from`} className={LABEL}>
              From
            </label>
            <input
              id={`${id}-from`}
              type="date"
              value={filters.from}
              max={filters.to || undefined}
              onChange={(e) => setFrom(e.target.value)}
              className={FIELD}
            />
          </div>
          <div className="min-w-0">
            <label htmlFor={`${id}-to`} className={LABEL}>
              To
            </label>
            <input
              id={`${id}-to`}
              type="date"
              value={filters.to}
              min={filters.from || undefined}
              onChange={(e) => setTo(e.target.value)}
              className={FIELD}
            />
          </div>
          <div className="min-w-0">
            <label htmlFor={`${id}-size`} className={LABEL}>
              Size
            </label>
            <select
              id={`${id}-size`}
              value={filters.size ?? ''}
              onChange={(e) => onChange({ size: (e.target.value || null) as SizeBucket | null })}
              className={FIELD}
            >
              <option value="">Any size</option>
              {SIZE_BUCKETS.map((bucket) => (
                <option key={bucket} value={bucket}>
                  {SIZE_LABEL[bucket]}
                </option>
              ))}
            </select>
          </div>
          <div className="min-w-0">
            <label htmlFor={`${id}-sort`} className={LABEL}>
              Sort by
            </label>
            <select
              id={`${id}-sort`}
              value={filters.sort}
              onChange={(e) => onChange({ sort: e.target.value as FileSort })}
              className={FIELD}
            >
              {SORTS.map((sort) => (
                <option key={sort} value={sort}>
                  {SORT_LABEL[sort]}
                </option>
              ))}
            </select>
          </div>
        </div>

        {active && (
          <div className="mt-4 flex justify-end">
            <button type="button" onClick={onClear} className={myFilesActionClass}>
              Clear filters
            </button>
          </div>
        )}
      </div>
    </form>
  );
}
