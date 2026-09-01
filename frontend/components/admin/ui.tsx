'use client';

/**
 * The admin area's small shared vocabulary: stat tiles, skeleton lines,
 * error panels and the standard page header. Every class here is lifted from
 * the design map (bordered rounded-ts cards on bg-surface, tiny uppercase
 * labels, danger/10 error surfaces) so the pages stay one visual system.
 */

import type { ReactNode } from 'react';

export function SkeletonLine({ className = 'w-24' }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={`inline-block h-3 animate-pulse rounded bg-surface-2 ${className}`}
    />
  );
}

export function StatTile({
  label,
  value,
  loading,
}: {
  label: string;
  value: number | string | undefined;
  loading?: boolean;
}) {
  return (
    <div className="rounded-ts border border-border bg-surface p-4">
      <div className="text-[11px] font-medium uppercase tracking-wide text-faint">
        {label}
      </div>
      <div className="mt-1.5 text-xl font-semibold tabular-nums text-ink">
        {loading ? (
          <SkeletonLine className="w-12" />
        ) : value === undefined ? (
          <span className="text-faint">—</span>
        ) : (
          typeof value === 'number' ? value.toLocaleString() : value
        )}
      </div>
    </div>
  );
}

export function ErrorPanel({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center justify-between gap-3 rounded-ts border border-danger/40 bg-danger/10 px-4 py-3"
    >
      <p className="text-sm text-danger">{message}</p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="rounded-lg border border-border bg-surface px-3 py-1.5 text-sm text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
        >
          Retry
        </button>
      )}
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-ink">
          {title}
        </h1>
        {subtitle && <p className="mt-1 text-sm text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

/** First letter of the name in a neutral circle — the admin list's avatar. */
export function AvatarInitial({
  name,
  size = 'sm',
}: {
  name: string;
  size?: 'sm' | 'lg';
}) {
  const initial = (name || '?').trim().charAt(0).toUpperCase() || '?';
  return (
    <span
      aria-hidden
      className={`flex shrink-0 items-center justify-center rounded-full bg-surface-2 font-semibold text-muted ${
        size === 'lg' ? 'h-12 w-12 text-lg' : 'h-8 w-8 text-xs'
      }`}
    >
      {initial}
    </span>
  );
}
