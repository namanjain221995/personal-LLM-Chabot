'use client';

/**
 * Deep Research analytics.
 *
 * Every figure comes from `research_runs`, which the research engine has
 * written since V11: one row per run with its outcome, its loop count, the
 * queries it issued and the sources it cited. Nothing here is inferred from
 * message text.
 */

import { AnalyticsChart } from '@/components/admin/analytics/AnalyticsChart';
import { ConsoleHeader, RangePicker, useRange } from '@/components/admin/analytics/filters';
import {
  ChartFrame,
  Section,
  Stat,
  StatRow,
  TopPeople,
} from '@/components/admin/analytics/ui';
import {
  compact,
  durationFromSeconds,
  exact,
  percent,
} from '@/components/admin/analytics/format';
import { useAnalytics } from '@/components/admin/analytics/useAnalytics';
import type { ResearchAnalytics } from '@/components/admin/analytics/types';

export default function ResearchAnalyticsPage() {
  const [range] = useRange();
  const { data, loading, error, reload } = useAnalytics<ResearchAnalytics>(
    'analytics/research',
    { range },
  );
  const t = data?.totals;
  const series = data?.series ?? [];

  return (
    <>
      <ConsoleHeader
        title="Deep research"
        description="Multi-step research runs: how many, how they ended, and what they read."
      >
        <RangePicker />
      </ConsoleHeader>

      <Section
        first
        title="Research runs"
        value={compact(t?.runs ?? null)}
        valueTitle={exact(t?.runs ?? null)}
        delta={data?.deltas.runs ?? null}
        stamp={t ? `${percent(t.success_rate)} completed` : undefined}
      >
        <ChartFrame
          height={230}
          loading={loading && !data}
          error={error}
          onRetry={reload}
          empty={!loading && series.every((p) => p.runs === 0)}
          emptyMessage="No research runs in this period."
        >
          <AnalyticsChart
            labels={series.map((p) => p.bucket)}
            bucket={data?.range.bucket ?? 'day'}
            height={230}
            ariaLabel="Research runs over time"
            series={[
              { name: 'Completed', bar: true, data: series.map((p) => p.completed) },
              {
                name: 'Failed',
                bar: true,
                tone: 'danger',
                data: series.map((p) => p.failed),
              },
            ]}
          />
        </ChartFrame>
      </Section>

      <Section title="Outcomes">
        <StatRow columns={5}>
          <Stat label="Completed" value={compact(t?.completed ?? null)} />
          <Stat label="Failed" value={compact(t?.failed ?? null)} />
          <Stat label="Cancelled" value={compact(t?.cancelled ?? null)} />
          <Stat label="Still running" value={compact(t?.running ?? null)} />
          <Stat label="People" value={compact(t?.users ?? null)} />
        </StatRow>
      </Section>

      <Section
        title="Effort per run"
        hint="A run loops until the evidence answers the question, so these describe how hard the questions were."
      >
        <StatRow columns={6}>
          <Stat
            label="Average duration"
            value={durationFromSeconds(t?.avg_seconds ?? null)}
          />
          <Stat
            label="P95 duration"
            value={durationFromSeconds(t?.p95_seconds ?? null)}
          />
          <Stat
            label="Loops"
            value={t?.avg_iterations == null ? '—' : t.avg_iterations.toFixed(1)}
          />
          <Stat
            label="Searches per run"
            value={t?.avg_queries == null ? '—' : t.avg_queries.toFixed(1)}
          />
          <Stat
            label="Sources cited"
            value={t?.avg_sources_cited == null ? '—' : t.avg_sources_cited.toFixed(1)}
            sub={
              t?.avg_sources_found == null
                ? undefined
                : `of ${t.avg_sources_found.toFixed(0)} found`
            }
          />
          <Stat
            label="Report length"
            value={
              t?.avg_report_chars == null
                ? '—'
                : `${compact(t.avg_report_chars)} chars`
            }
          />
        </StatRow>
      </Section>

      <Section title="Totals across the period">
        <StatRow columns={3}>
          <Stat label="Search queries issued" value={compact(t?.queries ?? null)} />
          <Stat label="Citations produced" value={compact(t?.citations ?? null)} />
          <Stat
            label="Success rate"
            value={percent(t?.success_rate ?? null)}
            sub={t ? `${t.completed} of ${t.runs} runs` : undefined}
          />
        </StatRow>
      </Section>

      <Section title="Who runs research">
        <TopPeople
          loading={loading && !data}
          rows={data?.top_users ?? []}
          pick={(r) => r.research_runs}
          unit="runs"
          emptyMessage="Nobody ran deep research in this period."
        />
      </Section>
    </>
  );
}
