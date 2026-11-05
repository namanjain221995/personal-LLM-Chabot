'use client';

/**
 * Salesforce analytics — METADATA ONLY.
 *
 * How many CRM questions were asked, how they were served (structured queries
 * or dataset answers), and how often they failed. Deliberately no record
 * content, no object names, no field names and no query text: reading a
 * member's Salesforce answers is a different capability with its own audit
 * trail, and an operations dashboard is not the place to leak it.
 *
 * The API's `live` and `synced` totals are not drawn. No writer ever stored the
 * value `live` counts, so it is always 0 and `synced` always equals `answers`,
 * older answers that really read the org included.
 */

import { AnalyticsChart } from '@/components/admin/analytics/AnalyticsChart';
import { ConsoleHeader, RangePicker, useRange } from '@/components/admin/analytics/filters';
import { ChartFrame, Section, Stat, StatRow } from '@/components/admin/analytics/ui';
import { compact, exact, percent } from '@/components/admin/analytics/format';
import { useAnalytics } from '@/components/admin/analytics/useAnalytics';
import type { SalesforceAnalytics } from '@/components/admin/analytics/types';

export default function SalesforceAnalyticsPage() {
  const [range] = useRange();
  const { data, loading, error, reload } = useAnalytics<SalesforceAnalytics>(
    'analytics/salesforce',
    { range },
  );
  const t = data?.totals;
  const series = data?.series ?? [];

  return (
    <>
      <ConsoleHeader
        title="Salesforce"
        description="CRM questions asked of the platform. Aggregate usage only — no record content appears here."
      >
        <RangePicker />
      </ConsoleHeader>

      <Section
        first
        title="Salesforce answers"
        value={compact(t?.answers ?? null)}
        valueTitle={exact(t?.answers ?? null)}
        delta={data?.deltas.answers ?? null}
        stamp={t ? `${percent(t.success_rate)} answered without error` : undefined}
      >
        <ChartFrame
          height={230}
          loading={loading && !data}
          error={error}
          onRetry={reload}
          empty={!loading && series.every((p) => p.answers === 0)}
          emptyMessage="No Salesforce questions in this period."
        >
          <AnalyticsChart
            labels={series.map((p) => p.bucket)}
            bucket={data?.range.bucket ?? 'day'}
            height={230}
            ariaLabel="Salesforce answers over time"
            series={[{ name: 'Answers', area: true, data: series.map((p) => p.answers) }]}
          />
        </ChartFrame>
      </Section>

      <Section
        title="How the questions were served"
        hint="Answers come from the local warehouse; nothing queries the org now. Older answers may have read the org directly, and those were never counted separately."
      >
        <StatRow columns={3}>
          <Stat label="Structured queries" value={compact(t?.sql_route ?? null)} />
          <Stat label="Dataset answers" value={compact(t?.dataset_route ?? null)} />
        </StatRow>
      </Section>

      <Section title="Reliability and reach">
        <StatRow columns={4}>
          <Stat label="People asking" value={compact(t?.users ?? null)} />
          <Stat
            label="Failed answers"
            value={compact(t?.failed ?? null)}
            sub={t?.answers ? `of ${exact(t.answers)}` : undefined}
          />
          <Stat label="Success rate" value={percent(t?.success_rate ?? null)} />
          <Stat
            label="Plans built"
            value={compact(t?.intents ?? null)}
            sub="query plans the planner produced"
          />
        </StatRow>
      </Section>
    </>
  );
}
