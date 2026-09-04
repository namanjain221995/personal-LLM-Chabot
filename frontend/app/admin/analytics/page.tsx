'use client';

/**
 * Usage — the analytics console's front page.
 *
 * Reading order is deliberate and matches the questions an operator actually
 * asks, in the order they ask them: who is using it (active people), how hard
 * (requests), what it cost (tokens), how much conversation that was
 * (messages), and how it felt (inference performance). The right rail answers
 * "who and what specifically" beside all of it.
 *
 * Every figure is real or absent. There is no sample data anywhere in this
 * console, and no metric is derived from a price list — this platform runs on
 * hardware in the building, so its units are requests, tokens, seconds and
 * watts, not currency.
 */

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { AnalyticsChart, type Series } from '@/components/admin/analytics/AnalyticsChart';
import {
  ConsoleHeader,
  ModelPicker,
  RangePicker,
  useQueryState,
  useRange,
} from '@/components/admin/analytics/filters';
import {
  ChartFrame,
  CoverageNote,
  RailEmpty,
  RailPanel,
  RailRow,
  RailSkeleton,
  Section,
  Stat,
  StatRow,
} from '@/components/admin/analytics/ui';
import {
  compact,
  duration,
  exact,
  percent,
  updatedAt,
} from '@/components/admin/analytics/format';
import { useAnalytics } from '@/components/admin/analytics/useAnalytics';
import type { MemberUsage, Overview } from '@/components/admin/analytics/types';
import { routeLabel } from '@/components/admin/analytics/types';

/** The rail's ranking metrics — every one of them a real local measurement. */
const RAIL_METRICS: {
  key: string;
  label: string;
  value: (m: MemberUsage) => number | null;
  render: (m: MemberUsage) => string;
}[] = [
  {
    key: 'output_tokens',
    label: 'Tokens generated',
    value: (m) => m.output_tokens,
    render: (m) => compact(m.output_tokens),
  },
  {
    key: 'requests',
    label: 'Requests',
    value: (m) => m.requests,
    render: (m) => compact(m.requests),
  },
  {
    key: 'messages',
    label: 'Messages',
    value: (m) => m.messages,
    render: (m) => compact(m.messages),
  },
  {
    key: 'research',
    label: 'Research runs',
    value: (m) => m.research_runs,
    render: (m) => compact(m.research_runs),
  },
  {
    key: 'web_searches',
    label: 'Web searches',
    value: (m) => m.web_searches,
    render: (m) => compact(m.web_searches),
  },
];

export default function UsageOverviewPage() {
  const [range] = useRange();
  const [model] = useQueryState('model', '');
  const { data, loading, error, reload } = useAnalytics<Overview>(
    'analytics/overview',
    { range, model },
  );
  // Tokens are the headline ranking, but they only exist from the day
  // telemetry was deployed. Until then the rail ranks by messages, which
  // reaches back through the whole history — a leaderboard of dashes on the
  // first day would read as a broken page rather than a young one.
  const [metricKey, setMetricKey] = useState<string | null>(null);
  const hasEvents = (data?.coverage.events ?? 0) > 0;
  const metric =
    RAIL_METRICS.find((m) => m.key === (metricKey ?? (hasEvents ? 'output_tokens' : 'messages'))) ??
    RAIL_METRICS[0]!;

  // When THIS browser last received the numbers — not a server timestamp,
  // which would keep reading "up to date" while a tab sat open all night.
  const [stamp, setStamp] = useState('');
  useEffect(() => {
    if (data) setStamp(updatedAt(new Date()));
  }, [data]);

  const bucket = data?.range.bucket ?? 'day';
  const activity = useMemo(() => data?.series.active_users ?? [], [data]);
  const usage = data?.series.usage ?? [];
  const labels = activity.map((p) => p.bucket);
  const usageLabels = usage.map((p) => p.bucket);

  // A series that is flat zero for the whole window is not information — it
  // is a legend entry and a line along the axis. Feature splits appear only
  // once the workspace actually uses that feature.
  const activeSeries = useMemo<Series[]>(() => {
    const candidates: [string, (p: (typeof activity)[number]) => number, boolean][] = [
      ['Active people', (p) => p.active, true],
      ['Chat', (p) => p.chat, false],
      ['Deep research', (p) => p.research, false],
      ['Web search', (p) => p.web_search, false],
      ['Salesforce', (p) => p.salesforce, false],
    ];
    return candidates
      .filter(([, pick], i) => i === 0 || activity.some((p) => pick(p) > 0))
      .map(([name, pick, area]) => ({
        name,
        area,
        data: activity.map(pick),
      }));
  }, [activity]);

  const totals = data?.totals;
  // Failed AND cancelled: the stamp says both, so the number must mean both.
  // `totals.errors` alone is failures — a cancellation is someone pressing
  // Stop, which is the platform working.
  const errorRate =
    totals && totals.requests > 0
      ? ((totals.errors + totals.cancelled) / totals.requests) * 100
      : null;

  const leaders = useMemo(() => data?.top_users ?? [], [data]);
  const ranked = useMemo(() => {
    const rows = [...leaders].sort(
      (a, b) => (metric.value(b) ?? 0) - (metric.value(a) ?? 0),
    );
    const top = metric.value(rows[0] ?? ({} as MemberUsage)) ?? 0;
    return rows
      .filter((r) => (metric.value(r) ?? 0) > 0)
      .slice(0, 10)
      .map((row) => ({ row, fraction: top > 0 ? (metric.value(row) ?? 0) / top : 0 }));
  }, [leaders, metric]);

  const models = data?.models ?? [];
  const modelTop = models[0]?.requests ?? 0;
  const routes = data?.routes ?? [];
  const routeTop = routes[0]?.requests ?? 0;

  return (
    <>
      <ConsoleHeader
        title="Usage"
        description={`Platform activity for ${data?.workspace.name ?? 'this workspace'} — every figure measured on this deployment.`}
      >
        <ModelPicker models={data?.available_models ?? []} />
        <RangePicker />
      </ConsoleHeader>

      <div className="grid grid-cols-1 gap-x-8 xl:grid-cols-[minmax(0,1fr)_336px]">
        <div className="min-w-0 xl:border-r xl:border-[var(--admin-separator)] xl:pr-8">
          {data && (
            <CoverageNote
              firstEvent={data.coverage.first_event}
              events={data.coverage.events}
              since={data.range.since}
            />
          )}

          <Section
            first
            title="Daily active users"
            hint="People who sent at least one message in the period, and the features they used. Counted from conversation history, so it covers the full life of the workspace."
            value={compact(
              activity.length ? Math.max(...activity.map((p) => p.active)) : null,
            )}
            unit="peak in a day"
            valueTitle="The highest number of people active in a single day of this period"
            stamp={stamp}
          >
            <ChartFrame
              height={230}
              loading={loading && !data}
              error={error}
              onRetry={reload}
              empty={!loading && activity.every((p) => p.active === 0)}
              emptyMessage="Nobody used the platform in this period."
            >
              <AnalyticsChart
                labels={labels}
                bucket={bucket}
                series={activeSeries}
                height={230}
                ariaLabel="Daily active users over time"
              />
            </ChartFrame>
          </Section>

          <Section
            title="AI requests"
            hint="One request is one model turn — a question answered, whatever engine served it. Cancelled and failed turns are counted too, which is what makes the error rate real."
            value={compact(totals?.requests ?? null)}
            valueTitle={exact(totals?.requests ?? null)}
            delta={data?.deltas.requests ?? null}
            stamp={
              errorRate == null
                ? undefined
                : `${percent(errorRate)} failed or cancelled`
            }
          >
            <ChartFrame
              height={200}
              loading={loading && !data}
              error={error}
              onRetry={reload}
              empty={!loading && usage.every((p) => p.requests === 0)}
              emptyMessage="No requests recorded in this period."
            >
              <AnalyticsChart
                labels={usageLabels}
                bucket={bucket}
                height={200}
                ariaLabel="AI requests over time"
                series={[
                  {
                    name: 'Requests',
                    area: true,
                    data: usage.map((p) => p.requests),
                  },
                  {
                    name: 'Failed',
                    tone: 'danger',
                    data: usage.map((p) => p.errors),
                  },
                ]}
              />
            </ChartFrame>
          </Section>

          <Section
            title="Tokens"
            hint="Exact counts reported by the serving runtime — prompt tokens from the tokenizer, completion tokens from the stream. Reasoning tokens are included in output."
            value={compact(totals?.total_tokens ?? null)}
            valueTitle={exact(totals?.total_tokens ?? null)}
            delta={data?.deltas.total_tokens ?? null}
            stamp={
              totals?.input_tokens == null
                ? undefined
                : `${compact(totals.input_tokens)} in · ${compact(totals.output_tokens)} out`
            }
          >
            <ChartFrame
              height={200}
              loading={loading && !data}
              error={error}
              onRetry={reload}
              empty={
                !loading && usage.every((p) => (p.output_tokens ?? 0) === 0)
              }
              emptyMessage="No token telemetry in this period."
            >
              <AnalyticsChart
                labels={usageLabels}
                bucket={bucket}
                height={200}
                stacked
                ariaLabel="Tokens processed over time"
                series={[
                  {
                    name: 'Input',
                    area: true,
                    data: usage.map((p) => p.input_tokens),
                    format: exact,
                  },
                  {
                    name: 'Output',
                    area: true,
                    data: usage.map((p) => p.output_tokens),
                    format: exact,
                  },
                ]}
              />
            </ChartFrame>
          </Section>

          <Section
            title="Messages"
            hint="Questions people asked, and the conversations they belong to. Read from history, so it is complete for the whole period."
            value={compact(data?.chat.messages ?? null)}
            valueTitle={exact(data?.chat.messages ?? null)}
            delta={data?.deltas.messages ?? null}
            stamp={
              data
                ? `${compact(data.chat.conversations)} conversations · ${compact(data.chat.users)} people`
                : undefined
            }
          >
            <ChartFrame
              height={200}
              loading={loading && !data}
              error={error}
              onRetry={reload}
              empty={!loading && activity.every((p) => p.messages === 0)}
              emptyMessage="No messages in this period."
            >
              <AnalyticsChart
                labels={labels}
                bucket={bucket}
                height={200}
                ariaLabel="Messages over time"
                series={[
                  {
                    name: 'Messages',
                    area: true,
                    data: activity.map((p) => p.messages),
                  },
                ]}
              />
            </ChartFrame>
          </Section>

          <Section
            title="Inference performance"
            hint="Measured by the orchestrator, so it includes retrieval, reranking and every pre-pass — the wait a person actually experiences, not the engine's own figure."
            actions={
              <Link
                href="/admin/analytics/performance"
                className="text-xs text-accent hover:underline"
              >
                Full performance view
              </Link>
            }
          >
            <StatRow columns={6}>
              <Stat
                label="Median first token"
                value={duration(totals?.p50_ttft_ms ?? null)}
              />
              <Stat
                label="P95 first token"
                value={duration(totals?.p95_ttft_ms ?? null)}
              />
              <Stat
                label="P99 first token"
                value={duration(totals?.p99_ttft_ms ?? null)}
              />
              <Stat
                label="Median response"
                value={duration(totals?.p50_duration_ms ?? null)}
              />
              <Stat
                label="Throughput"
                value={
                  totals?.avg_tokens_per_second == null
                    ? '—'
                    : `${totals.avg_tokens_per_second.toFixed(1)} tok/s`
                }
                sub="average per request"
              />
              <Stat
                label="Failure rate"
                value={percent(errorRate)}
                sub={
                  totals ? `${exact(totals.errors + totals.cancelled)} of ${exact(totals.requests)}` : undefined
                }
              />
            </StatRow>
          </Section>
        </div>

        {/* The rail. Below the charts under 1280px, beside them above it. */}
        <aside
          aria-label="Leaderboards"
          className="min-w-0 border-t border-[var(--admin-separator)] pt-2 xl:border-t-0 xl:pt-0"
        >
          <RailPanel
            title="People"
            hint="Ranked by the selected measurement, over the same period."
            action={
              <Link
                href={`/admin/analytics/leaderboards?range=${range}`}
                className="inline-flex h-8 items-center rounded-lg border border-[var(--admin-separator)] px-3 text-xs text-ink transition-colors hover:bg-[var(--admin-row-hover)]"
              >
                View full leaderboard
              </Link>
            }
          >
            <label className="sr-only" htmlFor="rail-metric">
              Rank people by
            </label>
            <select
              id="rail-metric"
              value={metric.key}
              onChange={(e) => setMetricKey(e.target.value)}
              className="mb-2 h-8 w-full appearance-none rounded-lg bg-[var(--admin-control)] px-2.5 text-xs text-ink transition-colors hover:bg-[var(--admin-control-hover)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              {RAIL_METRICS.map((m) => (
                <option key={m.key} value={m.key}>
                  {m.label}
                </option>
              ))}
            </select>
            {loading && !data ? (
              <RailSkeleton />
            ) : ranked.length === 0 ? (
              <RailEmpty>No {metric.label.toLowerCase()} in this period.</RailEmpty>
            ) : (
              <ul className="space-y-0.5">
                {ranked.map(({ row, fraction }, i) => (
                  <RailRow
                    key={row.id}
                    index={i}
                    avatar
                    label={row.name}
                    sublabel={row.email}
                    value={metric.render(row)}
                    valueTitle={exact(metric.value(row))}
                    fraction={fraction}
                  />
                ))}
              </ul>
            )}
          </RailPanel>

          <RailPanel
            title="Model usage"
            hint="Which locally deployed model served the requests."
          >
            {loading && !data ? (
              <RailSkeleton rows={3} />
            ) : models.length === 0 ? (
              <RailEmpty>
                No model telemetry yet. It begins with the next request.
              </RailEmpty>
            ) : (
              <ul className="space-y-0.5">
                {models.slice(0, 6).map((m, i) => (
                  <RailRow
                    key={m.model}
                    index={i}
                    label={m.model.split('/').pop() ?? m.model}
                    sublabel={m.model.includes('/') ? m.model.split('/')[0] : undefined}
                    value={m.share == null ? compact(m.requests) : `${m.share}%`}
                    valueTitle={`${exact(m.requests)} requests`}
                    fraction={modelTop > 0 ? m.requests / modelTop : 0}
                  />
                ))}
              </ul>
            )}
          </RailPanel>

          <RailPanel
            title="Feature mix"
            hint="Which engine answered — the platform's own routing, not a guess."
          >
            {loading && !data ? (
              <RailSkeleton rows={4} />
            ) : routes.length === 0 ? (
              <RailEmpty>No requests recorded in this period.</RailEmpty>
            ) : (
              <ul className="space-y-0.5">
                {routes.slice(0, 7).map((r, i) => (
                  <RailRow
                    key={r.route}
                    index={i}
                    label={routeLabel(r.route)}
                    value={compact(r.requests)}
                    valueTitle={exact(r.requests)}
                    fraction={routeTop > 0 ? r.requests / routeTop : 0}
                  />
                ))}
              </ul>
            )}
          </RailPanel>
        </aside>
      </div>
    </>
  );
}
