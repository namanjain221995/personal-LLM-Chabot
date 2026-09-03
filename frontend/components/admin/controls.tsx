'use client';

/**
 * The admin toolbar's controls — search, filter selects, tabs, buttons.
 *
 * They exist as one file because their whole job is to AGREE: the same
 * 40px height, the same 10px radius, the same border and hover, the same
 * focus ring. A toolbar where the search box is two pixels taller than the
 * filter beside it is the difference between "settings screen" and
 * "enterprise product", and that agreement cannot survive being retyped in
 * five pages.
 *
 * The filter is a NATIVE <select> with its arrow suppressed and ours drawn
 * on top: a listbox rebuilt in divs would cost keyboard support, screen
 * reader semantics, mobile pickers and type-ahead for a chevron.
 */

import type { ReactNode } from 'react';
import { IconChevronDown, IconSearch } from '@/components/icons';

/** One height for every toolbar control. */
export const CONTROL_HEIGHT = 'h-10';

const CONTROL_BASE =
  `${CONTROL_HEIGHT} rounded-ts border border-border text-sm text-ink transition-colors duration-ts`;

export const ADMIN_PRIMARY_BUTTON =
  `${CONTROL_HEIGHT} inline-flex shrink-0 items-center gap-2 rounded-ts bg-accent-strong px-3.5 text-sm font-medium text-white transition-all duration-ts hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg disabled:cursor-not-allowed disabled:opacity-40`;

export const ADMIN_SECONDARY_BUTTON =
  `${CONTROL_HEIGHT} inline-flex shrink-0 items-center gap-2 rounded-ts border border-border bg-[var(--admin-control)] px-3.5 text-sm font-medium text-muted transition-colors duration-ts hover:bg-[var(--admin-control-hover)] hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg disabled:cursor-not-allowed disabled:opacity-40`;

export function AdminSearchInput({
  value,
  onChange,
  placeholder,
  label,
  className = '',
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder: string;
  /** Accessible name — the field carries no visible <label>. */
  label: string;
  className?: string;
}) {
  return (
    <div
      className={`${CONTROL_BASE} flex min-w-0 items-center gap-2 bg-[var(--admin-control)] px-3 focus-within:border-accent/70 ${className}`}
    >
      <IconSearch size={15} className="shrink-0 text-faint" />
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={label}
        type="search"
        className="min-w-0 flex-1 bg-transparent text-sm text-ink placeholder:text-faint focus:outline-none [&::-webkit-search-cancel-button]:appearance-none"
      />
    </div>
  );
}

export function AdminSelect({
  value,
  onChange,
  label,
  options,
}: {
  value: string;
  onChange: (next: string) => void;
  /** Accessible name; also the "everything" option's text. */
  label: string;
  options: { value: string; label: string }[];
}) {
  return (
    <div className="relative shrink-0">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
        className={`${CONTROL_BASE} w-full cursor-pointer appearance-none bg-[var(--admin-control)] pl-3 pr-8 hover:bg-[var(--admin-control-hover)] focus:border-accent/70 focus:outline-none`}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <IconChevronDown
        size={15}
        className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-faint"
      />
    </div>
  );
}

export interface AdminTab {
  id: string;
  label: string;
  /** Rendered as a small neutral count beside the label. */
  count?: number;
}

export function AdminTabs({
  tabs,
  active,
  onChange,
  label,
}: {
  tabs: AdminTab[];
  active: string;
  onChange: (id: string) => void;
  label: string;
}) {
  return (
    <div role="tablist" aria-label={label} className="flex gap-6 border-b border-border">
      {tabs.map((tab) => {
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={selected}
            onClick={() => onChange(tab.id)}
            className={`-mb-px flex items-center gap-2 border-b-2 pb-3 pt-1 text-sm font-medium transition-colors duration-ts focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg ${
              selected
                ? 'border-ink text-ink'
                : 'border-transparent text-muted hover:text-ink'
            }`}
          >
            {tab.label}
            {tab.count !== undefined && tab.count > 0 && (
              <span className="rounded-full bg-surface-2 px-1.5 py-px text-[11px] font-medium tabular-nums text-muted">
                {tab.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/**
 * The toolbar strip: filters on the left, the page's one action on the
 * right. Wraps to two lines below `sm` without either half losing its
 * internal alignment.
 */
export function AdminToolbar({
  children,
  action,
}: {
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
        {children}
      </div>
      {action}
    </div>
  );
}
