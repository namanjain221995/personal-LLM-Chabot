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
import { AdminTable, type AdminColumn } from '@/components/admin/AdminTable';
import { AdminSelect } from '@/components/admin/controls';
import { ErrorPanel } from '@/components/admin/ui';
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

/**
 * Bounds a toolbar AdminSelect to the toolbar's width.
 *
 * The shared AdminSelect is `shrink-0` and sizes itself to its longest option,
 * so one long project name ("conformance-node-2026-09-13T07-52-43-441Z · Test")
 * made the Usage picker 396px wide in a 328px toolbar and pushed the whole
 * page 52px sideways on a phone (re-audit, 2026-09-13). Wrapped in this, the
 * select truncates its label instead. Every console toolbar select uses it.
 */
export const SELECT_FIT =
  'min-w-0 max-w-full [&>div]:max-w-full [&_select]:max-w-full [&_select]:truncate';

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
    <div data-testid="project-select" className={SELECT_FIT}>
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
 * The project-scoped panels' answer when the PROJECT LIST itself failed.
 *
 * Keys, Request logs, Webhooks and Limits all start from `GET /projects`. When
 * that request failed they used to read the empty list as "you have no
 * projects" — so an orchestrator restart told an admin their projects were
 * gone, with no way to try again (audit, 2026-09-13). An error is an error:
 * the server's sentence and a Retry, the way Usage and Overview already did.
 */
export function ProjectsLoadError({
  query,
}: {
  query: { error: string | null; reload: () => void };
}) {
  if (!query.error) return null;
  return <ErrorPanel message={query.error} onRetry={query.reload} />;
}

/**
 * A console table column. `hideBelow` widens AdminTable's own `hideBelowLg`
 * to a larger breakpoint for a column the laptop widths cannot afford.
 */
export type ConsoleColumn<T> = AdminColumn<T> & {
  /**
   * Hidden below this breakpoint as well as below lg: `xl` is 1280px, `2xl`
   * 1536px. Implies `hideBelowLg`.
   */
  hideBelow?: 'xl' | '2xl';
};

/**
 * The admin table, fitted to the console's column.
 *
 * VISUAL QA, 2026-09-13. At 400px the keys table showed its Key column and
 * nothing else: `minWidth={920}` is a DESKTOP floor, so on a phone the table
 * stayed 920px wide inside a sideways scroller and the row menu — the only way
 * to revoke a leaked key — sat 500px off the right edge with no hint it was
 * there. Projects, webhooks, logs and models had the same defect.
 *
 * THE FLOOR IS LIFTED BELOW xl, not lg (responsive audit, 2026-09-13). From
 * 1024px the 240px rail and the 64px of padding leave the table 720px, and
 * from lg to about 1363px every console table with a 920–1060px floor was cut
 * off on the right inside its scroller — the logs table hid four columns at
 * 1024 and one at 1280, with nothing on screen to say it scrolled. Now the
 * floor only applies from xl (the column is 976px there), each table's floor
 * is at most what that column holds, and the columns a laptop cannot afford
 * are hidden until the width exists: `hideBelowLg` on a phone, `hideBelow:
 * 'xl'` / `'2xl'` in between. The console tests pin the arithmetic at every
 * width from 400px to 1536px.
 *
 * AdminTable is shared with the admin area and knows only `hideBelowLg`, so
 * the wider tiers are nth-child rules on this wrapper, spelled out below as
 * whole class names because Tailwind only generates what it can read.
 *
 * THE SCOPES OVERLAP AT 1440px WAS A SHIFTED COLGROUP, not only a long cell.
 * AdminTable gives a hidden-below-lg `<col>` the class `lg:table-cell`, and a
 * `<col>` whose display is `table-cell` is not a column to Chrome: it drops
 * out of the colgroup and every later width slides one track left. Measured
 * on the keys table: Scopes drew at Status's 110px, Status at Last used's
 * 150px, and the text ran over both. `lg:[&_col]:!table-column` puts the
 * `<col>` back to the display a column has; the nth-child rules below out-rank
 * it by specificity while their breakpoint holds.
 */
export const CONSOLE_TABLE_FIT = 'max-xl:[&_table]:!min-w-0 lg:[&_col]:!table-column';

/** Column position (1-based) → the class that hides it below xl. */
const HIDE_BELOW_XL: Record<number, string> = {
  2: 'max-xl:[&_col:nth-child(2)]:!hidden max-xl:[&_th:nth-child(2)]:!hidden max-xl:[&_td:nth-child(2)]:!hidden',
  3: 'max-xl:[&_col:nth-child(3)]:!hidden max-xl:[&_th:nth-child(3)]:!hidden max-xl:[&_td:nth-child(3)]:!hidden',
  4: 'max-xl:[&_col:nth-child(4)]:!hidden max-xl:[&_th:nth-child(4)]:!hidden max-xl:[&_td:nth-child(4)]:!hidden',
  5: 'max-xl:[&_col:nth-child(5)]:!hidden max-xl:[&_th:nth-child(5)]:!hidden max-xl:[&_td:nth-child(5)]:!hidden',
  6: 'max-xl:[&_col:nth-child(6)]:!hidden max-xl:[&_th:nth-child(6)]:!hidden max-xl:[&_td:nth-child(6)]:!hidden',
  7: 'max-xl:[&_col:nth-child(7)]:!hidden max-xl:[&_th:nth-child(7)]:!hidden max-xl:[&_td:nth-child(7)]:!hidden',
  8: 'max-xl:[&_col:nth-child(8)]:!hidden max-xl:[&_th:nth-child(8)]:!hidden max-xl:[&_td:nth-child(8)]:!hidden',
};

/** Column position (1-based) → the class that hides it below 2xl. */
const HIDE_BELOW_2XL: Record<number, string> = {
  2: 'max-2xl:[&_col:nth-child(2)]:!hidden max-2xl:[&_th:nth-child(2)]:!hidden max-2xl:[&_td:nth-child(2)]:!hidden',
  3: 'max-2xl:[&_col:nth-child(3)]:!hidden max-2xl:[&_th:nth-child(3)]:!hidden max-2xl:[&_td:nth-child(3)]:!hidden',
  4: 'max-2xl:[&_col:nth-child(4)]:!hidden max-2xl:[&_th:nth-child(4)]:!hidden max-2xl:[&_td:nth-child(4)]:!hidden',
  5: 'max-2xl:[&_col:nth-child(5)]:!hidden max-2xl:[&_th:nth-child(5)]:!hidden max-2xl:[&_td:nth-child(5)]:!hidden',
  6: 'max-2xl:[&_col:nth-child(6)]:!hidden max-2xl:[&_th:nth-child(6)]:!hidden max-2xl:[&_td:nth-child(6)]:!hidden',
  7: 'max-2xl:[&_col:nth-child(7)]:!hidden max-2xl:[&_th:nth-child(7)]:!hidden max-2xl:[&_td:nth-child(7)]:!hidden',
  8: 'max-2xl:[&_col:nth-child(8)]:!hidden max-2xl:[&_th:nth-child(8)]:!hidden max-2xl:[&_td:nth-child(8)]:!hidden',
};

/** The wrapper classes for a set of columns: the fit, plus each wider tier. */
export function consoleTableClass(columns: { hideBelow?: 'xl' | '2xl' }[]): string {
  const tiers = columns.flatMap((col, i) => {
    if (!col.hideBelow) return [];
    const table = col.hideBelow === 'xl' ? HIDE_BELOW_XL : HIDE_BELOW_2XL;
    // Positions 2–8: the identity column (1) never hides, and no console
    // table has more than eight columns. The console tests assert every
    // declared tier produced its rule, so a ninth column cannot pass quietly.
    const rule = table[i + 1];
    return rule ? [rule] : [];
  });
  return [CONSOLE_TABLE_FIT, ...tiers].join(' ');
}

export function ConsoleTable<T>(
  props: Omit<Parameters<typeof AdminTable<T>>[0], 'columns'> & {
    columns: ConsoleColumn<T>[];
  },
) {
  const { columns, ...rest } = props;
  const adminColumns: AdminColumn<T>[] = columns.map(({ hideBelow, ...col }) =>
    hideBelow ? { ...col, hideBelowLg: true } : col,
  );
  return (
    <div data-testid="console-table" className={consoleTableClass(columns)}>
      <AdminTable {...rest} columns={adminColumns} />
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
