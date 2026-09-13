'use client';

/**
 * The pieces more than one console panel needs.
 *
 * Five panels are scoped to a project — keys, usage, logs, webhooks, limits —
 * and every one of them needs the same list, the same picker, the same "you
 * have no projects yet" answer. Written once here so they cannot drift into
 * five slightly different project selectors, which is the same reason the
 * admin toolbar controls live in one file.
 */

import type { ReactNode } from 'react';
import { AdminTable } from '@/components/admin/AdminTable';
import { AdminSelect } from '@/components/admin/controls';
import { ENVIRONMENT_LABEL, type Project } from './types';
import { useConsole } from './useConsole';
import { consolePaths } from './paths';

/**
 * A limit, in words. NULL INHERITS and ZERO MEANS ZERO (the shared limits
 * rule): null is "no value of its own, the platform default applies", and it
 * must never be drawn as 0, which is a real limit that allows nothing.
 */
export function limitText(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'Platform default';
  if (value === 0) return '0 — nothing allowed';
  return value.toLocaleString();
}

/**
 * Whether the orchestrator enforces the USAGE limits — requests per minute,
 * tokens per minute, the daily quota and per-project concurrency.
 *
 * OWNER DECISION 2026-09-13: the public API has no usage limits unless
 * PUBLIC_API_ENFORCE_LIMITS is set, and it defaults to false. The console must
 * then say Unlimited rather than draw a stored or default number that nothing
 * enforces — a figure that looks like a ceiling is an invented number.
 *
 * TOLERANT OF THE SHAPE on purpose: the flag is read as `enforced`,
 * `limits_enforced` or `enforce_limits`, on the object itself or on its
 * `limits` / `stats`, from the first source that carries a boolean. `null`
 * means no source said — a pre-switch orchestrator, which enforced every
 * limit, so callers treat null as enforced (see `limitsUnlimited`).
 */
export function limitsEnforcement(...sources: unknown[]): boolean | null {
  const flagOf = (value: unknown): boolean | null => {
    if (!value || typeof value !== 'object') return null;
    const record = value as Record<string, unknown>;
    for (const name of ['enforced', 'limits_enforced', 'enforce_limits']) {
      if (typeof record[name] === 'boolean') return record[name] as boolean;
    }
    return null;
  };
  for (const source of sources) {
    const own = flagOf(source);
    if (own !== null) return own;
    if (source && typeof source === 'object') {
      const record = source as Record<string, unknown>;
      const nested = flagOf(record.limits) ?? flagOf(record.stats);
      if (nested !== null) return nested;
    }
  }
  return null;
}

/** True only when the server SAID the limits are off; unknown stays enforced. */
export function limitsUnlimited(enforcement: boolean | null): boolean {
  return enforcement === false;
}

/**
 * A usage limit (rpm, tpm, daily quota, concurrency) in words: Unlimited when
 * the server said limits are not enforced, whatever number is stored; the
 * enforced wording of `limitText` otherwise.
 */
export function usageLimitText(
  value: number | null | undefined,
  enforcement: boolean | null,
): string {
  return limitsUnlimited(enforcement) ? 'Unlimited' : limitText(value);
}

/** The project list, shared by every project-scoped panel. */
export function useProjects() {
  return useConsole<{
    projects: Project[];
    limits_enforced?: boolean;
    enforced?: boolean;
  }>(consolePaths.projects());
}

/**
 * The project picker.
 *
 * It renders even for a single project, unlike the analytics ModelPicker that
 * hides itself below two options: here the selection is not a filter over one
 * dataset, it is WHICH project's keys you are about to revoke, and a
 * destructive action should always say out loud what it applies to.
 */
export function ProjectSelect({
  projects,
  value,
  onChange,
}: {
  projects: Project[];
  value: string;
  onChange: (next: string) => void;
}) {
  if (projects.length === 0) return null;
  // Clamped to the toolbar's width (2026-09-13, visual QA at 400 px). The
  // shared AdminSelect sizes itself to its longest option and will not shrink,
  // so a long project name pushed it over the toolbar's primary action and a
  // phone read "eate key". Bounding the chain here lets the select truncate
  // its label and the toolbar wrap the button onto its own line instead.
  return (
    <div
      data-testid="project-select"
      className="min-w-0 max-w-full [&>div]:max-w-full [&_select]:max-w-full [&_select]:truncate"
    >
      <AdminSelect
        value={value}
        onChange={onChange}
        label="Project"
        options={projects.map((p) => ({
          value: p.id,
          label: `${p.name} · ${ENVIRONMENT_LABEL[p.environment] ?? p.environment}`,
        }))}
      />
    </div>
  );
}

/**
 * Live or Test, in words.
 *
 * CONTRACT §5 makes the two environments distinguishable at a glance by the
 * key prefix; the console says it in a word as well, because "at a glance"
 * must not mean "by colour" for the person about to point a production
 * application at a test key.
 */
export function EnvironmentChip({ environment }: { environment: string }) {
  const live = environment === 'live';
  return (
    <span
      className={`inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium ${
        live
          ? 'border-accent/35 bg-accent/10 text-accent'
          : 'border-transparent text-muted'
      }`}
    >
      {ENVIRONMENT_LABEL[environment] ?? environment}
    </span>
  );
}

/**
 * The console's empty state.
 *
 * Every panel has one and none of them invents a number to avoid it: CONTRACT
 * §16 records a request only when one happens, so a console with no traffic
 * must say there is no traffic rather than draw a chart of zeros. An empty
 * state says what is missing, why, and what to do about it — a bare "No data"
 * tells a reader nothing they did not already know from the blank space.
 */
export function ConsoleEmpty({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-dashed border-[var(--admin-separator)] px-6 py-12 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      <p className="mx-auto mt-1.5 max-w-md text-xs leading-relaxed text-faint">
        {body}
      </p>
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  );
}

/**
 * The admin table, fitted to a phone.
 *
 * VISUAL QA, 2026-09-13. At 400px the keys table showed its Key column and
 * nothing else: `minWidth={920}` is a DESKTOP floor, so on a phone the table
 * stayed 920px wide inside a sideways scroller and the row menu — the only way
 * to revoke a leaked key — sat 500px off the right edge with no hint it was
 * there. Projects, webhooks, logs and models had the same defect.
 *
 * The floor now applies from `lg` up only (`!important` in the class is what
 * lets a stylesheet rule beat AdminTable's inline style). Below `lg` each
 * panel hides its secondary columns with `hideBelowLg`, so what is left — the
 * identity column, the status and the action — fits 368px with the identity
 * still readable. The scroller stays underneath as a backstop, not a layout.
 * The console tests pin the arithmetic for every table.
 *
 * THE SCOPES OVERLAP AT 1440px WAS A SHIFTED COLGROUP, not only a long cell.
 * AdminTable gives a hidden-below-lg `<col>` the class `lg:table-cell`, and a
 * `<col>` whose display is `table-cell` is not a column to Chrome: it drops
 * out of the colgroup and every later width slides one track left. Measured
 * on the keys table: Scopes drew at Status's 110px, Status at Last used's
 * 150px, and the text ran over both. `lg:[&_col]:!table-column` puts the
 * `<col>` back to the display a column has. (AdminTable itself is shared with
 * the admin area and not this file's to change; the same shift exists there.)
 */
export const CONSOLE_TABLE_FIT = 'max-lg:[&_table]:!min-w-0 lg:[&_col]:!table-column';

export function ConsoleTable<T>(props: Parameters<typeof AdminTable<T>>[0]) {
  return (
    <div className={CONSOLE_TABLE_FIT}>
      <AdminTable {...props} />
    </div>
  );
}

/**
 * A monospace value that can be copied — a key id, a request id, a URL.
 *
 * Wrapping rather than `truncate`: an identifier someone is trying to read
 * against a log line must be readable in full at 400px, and a row that grows
 * a line is a far smaller problem than an id that ends in an ellipsis.
 */
export function MonoValue({ value }: { value: string }) {
  // `whitespace-normal` because every console table cell is
  // `whitespace-nowrap`, and `break-all` inside nowrap cannot break at all —
  // the id simply ran on under the next column (visual QA, 2026-09-13).
  // `overflow-wrap: anywhere` rather than `break-all`: an id with no spaces
  // still breaks wherever it must, but "Chat · Streaming · Vision" breaks at
  // its spaces instead of drawing "Visi" / "on" on a phone.
  return (
    <code className="whitespace-normal font-mono text-xs text-muted [overflow-wrap:anywhere]">
      {value}
    </code>
  );
}
