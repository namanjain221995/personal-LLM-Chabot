'use client';

/**
 * Web search analytics.
 *
 * The backend is read from the data, not assumed: `providers` reports
 * whatever `web_searches.provider` actually contains on this deployment.
 *
 * The page store is GLOBAL shared knowledge — pages fetched for one person's
 * question become evidence for everyone's — so pages and domains are counted
 * platform-wide rather than per workspace, and the hint says so rather than
 * quietly mixing two scopes.
 */

import { AnalyticsChart } from '@/components/admin/analytics/AnalyticsChart';
import { ConsoleHeader, RangePicker, useRange } from '@/components/admin/analytics/filters';
import {
  BarList,
  ChartFrame,
  Section,
  Stat,
  StatRow,
  TopPeople,
} from '@/components/admin/analytics/ui';
import { compact, exact } from '@/components/admin/analytics/format';
import { useAnalytics } from '@/components/admin/analytics/useAnalytics';
import type { SearchAnalytics } from '@/components/admin/analytics/types';

export default function SearchAnalyticsPage() {
  const [range] = useRange();
  const { data, loading, error, reload } = useAnalytics<SearchAnalytics>(
    'analytics/search',
    { range },
  );
  const t = data?.totals;
  const series = data?.series ?? [];

  return (
    <>
      <ConsoleHeader
        title="Web search"
        description="Searches the platform ran, and the pages it read to answer them."
      >
        <RangePicker />
      </ConsoleHeader>

      <Section
        first
        title="Searches"
        hint="One search may fan out into several queries; both are counted."
        value={compact(t?.searches ?? null)}
        valueTitle={exact(t?.searches ?? null)}
        delta={data?.deltas.searches ?? null}
        stamp={t ? `${compact(t.queries)} queries issued` : undefined}
      >
        <ChartFrame
          height={230}
          loading={loading && !data}
          error={error}
          onRetry={reload}
          empty={!loading && series.every((p) => p.searches === 0)}
          emptyMessage="No web searches in this period."
        >
          <AnalyticsChart
            labels={series.map((p) => p.bucket)}
            bucket={data?.range.bucket ?? 'day'}
            height={230}
            ariaLabel="Web searches over time"
            series={[
              { name: 'Searches', area: true, data: series.map((p) => p.searches) },
            ]}
          />
        </ChartFrame>
      </Section>

      <Section title="What the searches returned">
        <StatRow columns={5}>
          <Stat label="Results" value={compact(t?.results ?? null)} />
          <Stat
            label="Results per search"
            value={
              t?.results_per_search == null ? '—' : t.results_per_search.toFixed(1)
            }
          />
          <Stat label="Unique links" value={compact(t?.unique_urls ?? null)} />
          <Stat
            label="Pages fetched"
            value={compact(t?.pages_fetched ?? null)}
            sub="platform-wide"
          />
          <Stat
            label="Domains read"
            value={compact(t?.domains ?? null)}
            sub="platform-wide"
          />
        </StatRow>
      </Section>

      <Section
        title="Search backends"
        hint="Read from the search log, so this is what actually served the queries."
      >
        <BarList
          loading={loading && !data}
          rows={(data?.providers ?? []).map((p) => ({
            label: p.provider,
            value: p.searches,
          }))}
          emptyMessage="No searches in this period."
        />
      </Section>

      <Section
        title="Most-read domains"
        hint="Pages the platform fetched and kept, across the whole deployment."
      >
        <BarList
          loading={loading && !data}
          rows={(data?.domains ?? []).map((d) => ({
            label: d.domain,
            value: d.pages,
          }))}
          emptyMessage="No pages fetched in this period."
        />
      </Section>

      <Section title="Who searches">
        <TopPeople
          loading={loading && !data}
          rows={data?.top_users ?? []}
          pick={(r) => r.web_searches}
          unit="searches"
          emptyMessage="Nobody used web search in this period."
        />
      </Section>
    </>
  );
}
