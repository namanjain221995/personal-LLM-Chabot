'use client';

/**
 * What the API has actually been asked to do.
 *
 * It reads `GET /usage` (`console_api.usage_series`), which sums the durable
 * per-project daily ledger — one project, or every project in the workspace
 * when none is chosen.
 *
 * THE WINDOW IS WHAT THE SERVER OFFERS (rewritten 2026-09-13). This panel
 * used to borrow the analytics RangePicker (1h/24h/7d/30d/90d) and send
 * `range=` and `project=` — two parameters the console router has never
 * read, so every choice silently returned the default thirty days. The router
 * takes `days` (1…93, a daily ledger) and `project_id`; the picker offers
 * whole-day windows inside that bound and nothing finer, because an hour is
 * a resolution this ledger does not have.
 *
 * A DAY WITH NO ROW IS NOT DRAWN AS ZERO. The server lists only days the
 * ledger holds; the chart's x axis is every day of the window the server
 * reports (`range.start`…`range.end`), and a day with no row is a GAP in the
 * line rather than an invented floor. Until 2026-09-13 the axis was only the
 * days that had rows, so traffic on the 1st and the 30th drew as two
 * consecutive points joined by a line (responsive audit).
 */

import { useEffect, useState } from 'react';
import { AnalyticsChart, type Series } from '@/components/admin/analytics/AnalyticsChart';
import { ConsoleHeader } from '@/components/admin/analytics/filters';
import {
  BarList,
  ChartFrame,
  Section,
  Stat,
  StatRow,
} from '@/components/admin/analytics/ui';
import { compact } from '@/components/admin/analytics/format';
import { AdminSelect, AdminToolbar } from '@/components/admin/controls';
import { ConsoleEmpty, SELECT_FIT, useProjects } from './shared';
import { useConsole } from './useConsole';
import { consolePaths } from './paths';
import { useConsoleStatus } from './status';
import { ENVIRONMENT_LABEL, type UsageReport } from './types';

/** Whole-day windows, all inside console_api.MAX_USAGE_DAYS (93). */
export const USAGE_WINDOWS = [
  { days: 7, label: 'Last 7 days' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
] as const;

/** One calendar day after `day` (YYYY-MM-DD), computed in UTC so no zone moves it. */
function nextDay(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

const DAY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * The chart's axis and values: every day from `range.start` to `range.end`
 * inclusive, with `null` — a gap — for a day the ledger has no row for.
 *
 * Labels are LOCAL midnight (`YYYY-MM-DDT00:00:00`), not the bare date: the
 * bare form parses as UTC midnight, which a viewer west of Greenwich reads as
 * the evening before, so every bar sat under the previous day's name.
 *
 * A report without a usable range (an older orchestrator) falls back to the
 * days it listed, which is what the chart drew before.
 */
export function usageDays(report: Pick<UsageReport, 'range' | 'series'> | null): {
  labels: string[];
  requests: (number | null)[];
  errors: (number | null)[];
} {
  const series = report?.series ?? [];
  const byDay = new Map(series.map((point) => [point.day, point]));
  const start = report?.range?.start;
  const end = report?.range?.end;
  let days: string[];
  if (start && end && DAY.test(start) && DAY.test(end) && start <= end) {
    days = [];
    // Bounded by the router's own 93-day ceiling, with room to spare, so a
    // malformed range can never spin this loop.
    for (let day = start; day <= end && days.length < 400; day = nextDay(day)) {
      days.push(day);
    }
  } else {
    days = series.map((point) => point.day);
  }
  return {
    labels: days.map((day) => (DAY.test(day) ? `${day}T00:00:00` : day)),
    requests: days.map((day) => byDay.get(day)?.requests ?? null),
    errors: days.map((day) => byDay.get(day)?.errors ?? null),
  };
}

export function UsagePanel() {
  const projectsQuery = useProjects();
  const projects = projectsQuery.data?.projects ?? [];
  // '' is "every project in this workspace", which the router supports.
  const [projectId, setProjectId] = useState('');
  const [days, setDays] = useState<number>(30);

  const usage = useConsole<UsageReport>(consolePaths.usage(), {
    project_id: projectId || undefined,
    days,
  });
  const { announce } = useConsoleStatus();

  const report = usage.data;
  const totals = report?.totals;
  const axis = usageDays(report);
  const hasTraffic = (totals?.requests ?? 0) > 0;
  const figure = (value: number | undefined) =>
    value === undefined ? (usage.loading ? '…' : '—') : compact(value);

  useEffect(() => {
    if (usage.loading) announce('Loading usage.');
    else if (usage.error) announce(usage.error);
    else if (report) {
      announce(
        hasTraffic
          ? `${compact(totals?.requests ?? 0)} requests in this window.`
          : 'No API requests in this window.',
      );
    }
  }, [usage.loading, usage.error, report, hasTraffic, totals?.requests, announce]);

  const requestSeries: Series[] = [
    { name: 'Requests', data: axis.requests, area: true },
    { name: 'Errors', data: axis.errors, tone: 'danger' },
  ];

  if (!projectsQuery.loading && !projectsQuery.error && projects.length === 0) {
    return (
      <div>
        <ConsoleHeader title="Usage" />
        <ConsoleEmpty
          title="No projects to measure"
          body="Usage is recorded per project. Create a project and make a call with one of its keys, and the figures start here."
        />
      </div>
    );
  }

  return (
    <div>
      <ConsoleHeader
        title="Usage"
        description="Requests and tokens per day, from the durable usage ledger the quota gate writes. Nothing here is estimated."
      />

      <AdminToolbar>
        <div data-testid="usage-project-select" className={SELECT_FIT}>
          <AdminSelect
            value={projectId}
            onChange={setProjectId}
            label="Project"
            options={[
              { value: '', label: 'All projects' },
              ...projects.map((p) => ({
                value: p.id,
                label: `${p.name} · ${ENVIRONMENT_LABEL[p.environment] ?? p.environment}`,
              })),
            ]}
          />
        </div>
        <div className={SELECT_FIT}>
          <AdminSelect
            value={String(days)}
            onChange={(next) => setDays(Number(next))}
            label="Time range"
            options={USAGE_WINDOWS.map((w) => ({ value: String(w.days), label: w.label }))}
          />
        </div>
      </AdminToolbar>

      <Section title="Totals" first>
        <StatRow columns={6}>
          <Stat label="Requests" value={figure(totals?.requests)} />
          <Stat label="Errors" value={figure(totals?.errors)} />
          <Stat label="Rate limited" value={figure(totals?.rate_limited)} />
          <Stat label="Input tokens" value={figure(totals?.input_tokens)} />
          <Stat label="Output tokens" value={figure(totals?.output_tokens)} />
          <Stat label="Total tokens" value={figure(totals?.total_tokens)} />
        </StatRow>
      </Section>

      <Section title="Requests per day">
        <ChartFrame
          height={260}
          loading={usage.loading && report === null}
          error={usage.error}
          empty={!hasTraffic}
          emptyMessage="No API requests in this window. The chart fills in from the first call."
          onRetry={usage.reload}
        >
          <AnalyticsChart
            labels={axis.labels}
            bucket="day"
            series={requestSeries}
            ariaLabel="API requests and errors per day"
          />
        </ChartFrame>
      </Section>

      {/* Not drawn when the report failed: an empty list there read "No
          project has served a request in this window" beside the chart's
          error — a measurement nobody made (audit, 2026-09-13). The chart
          above carries the error and its Retry. */}
      {!(usage.error && report === null) && (
        <Section title="By project">
          <BarList
            loading={usage.loading && report === null}
            rows={(report?.projects ?? [])
              .filter((row) => row.requests > 0)
              .map((row) => ({
                label: row.name,
                sublabel: `${compact(row.output_tokens)} output tokens`,
                value: row.requests,
              }))}
            emptyMessage="No project has served a request in this window."
          />
        </Section>
      )}
    </div>
  );
}
