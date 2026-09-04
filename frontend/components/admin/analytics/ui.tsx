'use client';

/**
 * The console's shared furniture: sections, stats, states, rails.
 *
 * The visual language is deliberately FLAT. Sections are separated by a
 * hairline, not boxed into cards — a page of twelve cards reads as twelve
 * unrelated widgets, where a ruled page reads as one report. Nothing here
 * draws a border unless it is separating two things that would otherwise
 * touch.
 *
 * Every data surface has four states and all four live here, because a page
 * that only implements "success" collapses its own layout the moment the
 * network is slow: `ChartFrame` holds its height while loading, so nothing
 * below it jumps when the data lands.
 */

import type { ReactNode } from 'react';
import { NOT_MEASURED, compact, exact, initialOf } from './format';
import type { Infra, MemberUsage } from './types';

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

/**
 * One analytics section: a title, an optional headline figure with its
 * change, an optional right-hand stamp, and the body.
 *
 * `divide` puts the hairline ABOVE the section, which is how a run of them
 * reads as one continuous page rather than a stack of panels.
 */
export function Section({
  title,
  hint,
  value,
  valueTitle,
  unit,
  delta,
  deltaGoodWhenDown = false,
  stamp,
  actions,
  children,
  first = false,
}: {
  title: string;
  hint?: string;
  value?: string;
  valueTitle?: string;
  /** What the headline number counts, when the title alone is ambiguous. */
  unit?: string;
  delta?: number | null;
  /** Latency and error rate improve by going DOWN; volume does not. */
  deltaGoodWhenDown?: boolean;
  stamp?: string;
  actions?: ReactNode;
  children: ReactNode;
  first?: boolean;
}) {
  return (
    <section
      className={`px-1 py-6 ${first ? '' : 'border-t border-[var(--admin-separator)]'}`}
    >
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-sm font-medium text-ink">
            {title}
            {hint && (
              <span
                title={hint}
                aria-label={hint}
                className="grid h-4 w-4 place-items-center rounded-full border border-[var(--admin-separator)] text-[9px] leading-none text-faint"
              >
                i
              </span>
            )}
          </h2>
          {value !== undefined && (
            <div className="mt-2 flex items-baseline gap-2.5">
              <span
                title={valueTitle}
                className="text-[28px] font-semibold leading-none tracking-tight text-ink [font-variant-numeric:tabular-nums]"
              >
                {value}
              </span>
              {unit && <span className="text-xs text-faint">{unit}</span>}
              <Delta value={delta} goodWhenDown={deltaGoodWhenDown} />
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-3">
          {actions}
          {stamp && <span className="text-xs text-faint">{stamp}</span>}
        </div>
      </div>
      {children}
    </section>
  );
}

/**
 * The change against the previous, equally long window.
 *
 * Renders NOTHING when the comparison is meaningless — a zero denominator or
 * an unmeasured window. An arrow that always appears is an arrow nobody
 * believes.
 */
export function Delta({
  value,
  goodWhenDown = false,
}: {
  value: number | null | undefined;
  goodWhenDown?: boolean;
}) {
  if (value == null || !Number.isFinite(value) || value === 0) return null;
  const up = value > 0;
  const good = goodWhenDown ? !up : up;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium [font-variant-numeric:tabular-nums] ${
        good ? 'bg-ok/15 text-ok' : 'bg-danger/15 text-danger'
      }`}
      title={`${up ? 'Up' : 'Down'} ${Math.abs(value).toFixed(1)}% against the previous period`}
    >
      <span aria-hidden>{up ? '↑' : '↓'}</span>
      {Math.abs(value).toFixed(1)}%
    </span>
  );
}

/** A small labelled number. The console's densest unit. */
export function Stat({
  label,
  value,
  sub,
  title,
}: {
  label: string;
  value: string;
  sub?: string;
  title?: string;
}) {
  return (
    <div className="min-w-0">
      <dt className="truncate text-xs text-faint">{label}</dt>
      <dd
        title={title}
        className="mt-1 truncate text-lg font-semibold leading-tight text-ink [font-variant-numeric:tabular-nums]"
      >
        {value}
      </dd>
      {sub && <p className="mt-0.5 truncate text-[11px] text-faint">{sub}</p>}
    </div>
  );
}

/** A responsive row of Stats — 2 up on a phone, 4 or 6 on a desktop. */
export function StatRow({
  children,
  columns = 4,
}: {
  children: ReactNode;
  columns?: 3 | 4 | 5 | 6;
}) {
  const lg = {
    3: 'lg:grid-cols-3',
    4: 'lg:grid-cols-4',
    5: 'lg:grid-cols-5',
    6: 'lg:grid-cols-6',
  }[columns];
  return (
    <dl className={`grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-3 ${lg}`}>
      {children}
    </dl>
  );
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

/**
 * Holds a chart's height through loading and empty, so the page below never
 * jumps. This is the single most noticeable difference between a dashboard
 * that feels solid and one that feels cheap.
 */
export function ChartFrame({
  height,
  loading,
  error,
  empty,
  emptyMessage = 'No activity in this period.',
  onRetry,
  children,
}: {
  height: number;
  loading?: boolean;
  error?: string | null;
  empty?: boolean;
  emptyMessage?: string;
  onRetry?: () => void;
  children: ReactNode;
}) {
  if (loading) {
    return (
      <div
        style={{ height }}
        aria-busy="true"
        aria-label="Loading chart"
        className="animate-pulse rounded-lg bg-[var(--admin-control)]"
      />
    );
  }
  if (error) {
    return (
      <div
        style={{ height }}
        className="flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-[var(--admin-separator)] text-center"
      >
        <p className="max-w-sm px-6 text-sm text-muted">{error}</p>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="rounded-lg border border-[var(--admin-separator)] px-3 py-1.5 text-xs text-ink transition-colors hover:bg-[var(--admin-row-hover)]"
          >
            Retry
          </button>
        )}
      </div>
    );
  }
  if (empty) {
    return (
      <div
        style={{ height }}
        className="flex items-center justify-center rounded-lg border border-dashed border-[var(--admin-separator)]"
      >
        <p className="px-6 text-center text-sm text-faint">{emptyMessage}</p>
      </div>
    );
  }
  return <>{children}</>;
}

/**
 * What the infrastructure blocks show when Prometheus is not answering.
 *
 * It says which collector and why, because "unavailable" alone sends an
 * operator hunting. It never renders zeros — a GPU reported at 0% because
 * nobody asked looks exactly like an idle GPU.
 */
export function TelemetryUnavailable({
  reason,
  source,
  what = 'This telemetry',
}: {
  reason: string;
  source?: string;
  what?: string;
}) {
  return (
    <div className="rounded-lg border border-dashed border-[var(--admin-separator)] p-6">
      <p className="text-sm text-ink">{what} is not available right now.</p>
      <p className="mt-1.5 max-w-xl text-xs leading-relaxed text-faint">
        It is read from Prometheus{source ? ` (${source})` : ''}, which did not
        answer: <span className="text-muted">{reason}</span>. Start the
        monitoring profile to see it — nothing is estimated in its place.
      </p>
    </div>
  );
}

/** The console-wide "telemetry starts here" note. */
export function CoverageNote({
  firstEvent,
  events,
  since,
}: {
  firstEvent: string | null;
  events: number;
  since: string;
}) {
  if (events > 0 && firstEvent && new Date(firstEvent) <= new Date(since)) {
    return null;
  }
  if (events === 0) {
    return (
      <p className="rounded-lg border border-dashed border-[var(--admin-separator)] px-4 py-3 text-xs leading-relaxed text-faint">
        Request, token and latency telemetry begins when this release is
        deployed — there are no events recorded yet. Message, research, search
        and Salesforce figures on this page come from the full history and are
        complete.
      </p>
    );
  }
  return (
    <p className="rounded-lg border border-dashed border-[var(--admin-separator)] px-4 py-3 text-xs leading-relaxed text-faint">
      Request and token telemetry starts{' '}
      {new Date(firstEvent as string).toLocaleDateString(undefined, {
        month: 'long',
        day: 'numeric',
      })}
      . Anything before that is not missing activity — it is activity recorded
      before this measurement existed.
    </p>
  );
}

// ---------------------------------------------------------------------------
// The right-hand rail
// ---------------------------------------------------------------------------

export function RailPanel({
  title,
  hint,
  action,
  children,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="border-t border-[var(--admin-separator)] px-1 py-6 first:border-t-0 first:pt-0 xl:border-t xl:px-5 xl:first:pt-6">
      <h2 className="flex items-center gap-2 text-sm font-medium text-ink">
        {title}
        {hint && (
          <span
            title={hint}
            aria-label={hint}
            className="grid h-4 w-4 place-items-center rounded-full border border-[var(--admin-separator)] text-[9px] leading-none text-faint"
          >
            i
          </span>
        )}
      </h2>
      <div className="mt-3">{children}</div>
      {action && <div className="mt-4">{action}</div>}
    </section>
  );
}

/**
 * One rail row: rank mark, name, value.
 *
 * The bar behind the row is proportional to the leader, which turns a list of
 * numbers into a shape you can read at a glance without adding a chart.
 */
export function RailRow({
  index,
  label,
  sublabel,
  value,
  valueTitle,
  fraction,
  avatar,
}: {
  index: number;
  label: string;
  sublabel?: string;
  value: string;
  valueTitle?: string;
  fraction: number;
  avatar?: boolean;
}) {
  return (
    <li className="relative flex h-11 items-center gap-2.5 rounded-lg px-2">
      <span
        aria-hidden
        className="absolute inset-y-0 left-0 rounded-lg bg-[var(--admin-row-hover)]"
        style={{ width: `${Math.max(2, Math.min(100, fraction * 100))}%` }}
      />
      {avatar ? (
        <span className="relative grid h-6 w-6 shrink-0 place-items-center rounded-full bg-[var(--admin-control)] text-[10px] font-semibold text-muted">
          {initialOf(label, sublabel)}
        </span>
      ) : (
        <span className="relative w-4 shrink-0 text-[11px] text-faint [font-variant-numeric:tabular-nums]">
          {index + 1}
        </span>
      )}
      <span className="relative min-w-0 flex-1">
        <span className="block truncate text-[13px] text-ink">{label}</span>
        {sublabel && (
          <span className="block truncate text-[11px] text-faint">
            {sublabel}
          </span>
        )}
      </span>
      <span
        title={valueTitle}
        className="relative shrink-0 text-[13px] font-medium text-ink [font-variant-numeric:tabular-nums]"
      >
        {value}
      </span>
    </li>
  );
}

/** The rail's own empty state — quieter than a chart's. */
export function RailEmpty({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-lg border border-dashed border-[var(--admin-separator)] px-3 py-6 text-center text-xs text-faint">
      {children}
    </p>
  );
}

/** Skeleton rows for the rail while it loads. */
export function RailSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <ul aria-busy="true" aria-label="Loading" className="space-y-1">
      {Array.from({ length: rows }, (_, i) => (
        <li
          key={i}
          className="h-11 animate-pulse rounded-lg bg-[var(--admin-control)]"
        />
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Small shared marks
// ---------------------------------------------------------------------------

/** A number rendered compact with its exact value one hover away. */
export function Num({ value }: { value: number | null | undefined }) {
  return (
    <span
      title={value == null ? undefined : exact(value)}
      className="[font-variant-numeric:tabular-nums]"
    >
      {compact(value)}
    </span>
  );
}

/** A health dot with its state in words — never colour alone. */
export function HealthMark({
  ok,
  okLabel = 'Healthy',
  badLabel = 'Down',
}: {
  ok: boolean;
  okLabel?: string;
  badLabel?: string;
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-muted">
      <span
        aria-hidden
        className={`h-1.5 w-1.5 rounded-full ${ok ? 'bg-ok' : 'bg-danger'}`}
      />
      {ok ? okLabel : badLabel}
    </span>
  );
}

/** A horizontal utilisation bar for the node cards. */
export function Meter({
  value,
  label,
  caution = 85,
}: {
  /** 0-100, or null for not measured. */
  value: number | null;
  label: string;
  /** Above this the bar warns. GPUs run hot on purpose; hosts do not. */
  caution?: number;
}) {
  const pct = value == null ? null : Math.max(0, Math.min(100, value));
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-faint">{label}</span>
        <span className="text-xs font-medium text-ink [font-variant-numeric:tabular-nums]">
          {pct == null ? NOT_MEASURED : `${pct.toFixed(0)}%`}
        </span>
      </div>
      <div
        className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-[var(--admin-control)]"
        role="img"
        aria-label={`${label}: ${pct == null ? 'not measured' : `${pct.toFixed(0)} percent`}`}
      >
        {pct != null && (
          <div
            className={`h-full rounded-full ${pct >= caution ? 'bg-warn' : 'bg-accent-strong'}`}
            style={{ width: `${pct}%` }}
          />
        )}
      </div>
    </div>
  );
}

/**
 * The "top people" list every feature page ends with.
 *
 * Shared so Chat, Research, Web search and Salesforce rank the same way and
 * look the same doing it — four hand-rolled lists is how four pages drift
 * apart. `pick` is what each page measures; everything else is fixed.
 */
export function TopPeople({
  rows,
  pick,
  unit,
  loading,
  emptyMessage,
}: {
  rows: MemberUsage[];
  pick: (row: MemberUsage) => number;
  /** Plural noun for the value — "messages", "runs", "searches". */
  unit: string;
  loading?: boolean;
  emptyMessage: string;
}) {
  if (loading) return <RailSkeleton />;
  const ranked = rows.filter((r) => pick(r) > 0).slice(0, 8);
  if (ranked.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-[var(--admin-separator)] px-4 py-6 text-center text-xs text-faint">
        {emptyMessage}
      </p>
    );
  }
  const top = pick(ranked[0]!);
  return (
    <ul className="max-w-xl space-y-0.5">
      {ranked.map((row, i) => (
        <RailRow
          key={row.id}
          index={i}
          avatar
          label={row.name}
          sublabel={row.email}
          value={`${compact(pick(row))} ${unit}`}
          valueTitle={exact(pick(row))}
          fraction={top > 0 ? pick(row) / top : 0}
        />
      ))}
    </ul>
  );
}

/**
 * A ranked bar list for non-people dimensions — providers, domains, routes.
 * Same shape as the rail rows, so the console has ONE way of showing "these
 * things, ordered, with their size".
 */
export function BarList({
  rows,
  emptyMessage,
  loading,
}: {
  rows: { label: string; sublabel?: string; value: number }[];
  emptyMessage: string;
  loading?: boolean;
}) {
  if (loading) return <RailSkeleton rows={4} />;
  if (rows.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-[var(--admin-separator)] px-4 py-6 text-center text-xs text-faint">
        {emptyMessage}
      </p>
    );
  }
  const top = Math.max(...rows.map((r) => r.value), 0);
  return (
    <ul className="max-w-xl space-y-0.5">
      {rows.map((row, i) => (
        <RailRow
          key={row.label}
          index={i}
          label={row.label}
          sublabel={row.sublabel}
          value={compact(row.value)}
          valueTitle={exact(row.value)}
          fraction={top > 0 ? row.value / top : 0}
        />
      ))}
    </ul>
  );
}

/**
 * Render an infrastructure block, or explain why it is not there.
 *
 * The three states — loading, unavailable, present — used to be spelled out
 * on every page that touched Prometheus, and TypeScript could not narrow the
 * union through a page-level ternary. This does the narrowing in one place,
 * which is also the only place that decides what "unavailable" looks like.
 */
export function InfraBlock<T>({
  state,
  what,
  skeletonHeight = 160,
  children,
}: {
  state: Infra<T> | undefined;
  what: string;
  skeletonHeight?: number;
  children: (value: T) => ReactNode;
}) {
  if (!state) {
    return (
      <div
        style={{ height: skeletonHeight }}
        aria-busy="true"
        aria-label={`Loading ${what.toLowerCase()}`}
        className="animate-pulse rounded-xl bg-[var(--admin-control)]"
      />
    );
  }
  if (!state.available) {
    return (
      <TelemetryUnavailable
        what={what}
        reason={state.reason}
        source={state.source}
      />
    );
  }
  return <>{children(state as unknown as T)}</>;
}
