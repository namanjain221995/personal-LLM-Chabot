'use client';

/**
 * Inference performance.
 *
 * The page exists to make ONE distinction legible: what a person waits for is
 * not what the engine reports. Our own telemetry measures from the moment the
 * request arrives — retrieval, reranking, the Salesforce planner and every
 * other pre-pass included — while vLLM's histograms start when the prompt
 * reaches the engine. Both are shown, side by side, and the gap between them
 * is the platform's own overhead, which is the number worth watching.
 */

import { AnalyticsChart } from '@/components/admin/analytics/AnalyticsChart';
import {
  ConsoleHeader,
  ModelPicker,
  RangePicker,
  useQueryState,
  useRange,
} from '@/components/admin/analytics/filters';
import {
  ChartFrame,
  Section,
  Stat,
  StatRow,
  TelemetryUnavailable,
} from '@/components/admin/analytics/ui';
import { AdminTable, type AdminColumn } from '@/components/admin/AdminTable';
import {
  NOT_MEASURED,
  compact,
  duration,
  durationFromSeconds,
  exact,
  percent,
} from '@/components/admin/analytics/format';
import { useAnalytics } from '@/components/admin/analytics/useAnalytics';
import {
  routeLabel,
  type PerformanceAnalytics,
  type RouteUsage,
} from '@/components/admin/analytics/types';

const ROUTE_COLUMNS: AdminColumn<RouteUsage>[] = [
  {
    key: 'route',
    label: 'Engine',
    render: (row) => (
      <span className="text-[13px] font-medium text-ink">
        {routeLabel(row.route)}
      </span>
    ),
  },
  {
    key: 'requests',
    label: 'Requests',
    width: '110px',
    align: 'right',
    render: (row) => <span title={exact(row.requests)}>{compact(row.requests)}</span>,
  },
  {
    key: 'ttft',
    label: 'Avg first token',
    width: '140px',
    align: 'right',
    render: (row) => duration(row.avg_ttft_ms),
  },
  {
    key: 'total',
    label: 'Avg response',
    width: '140px',
    align: 'right',
    render: (row) => duration(row.avg_duration_ms ?? null),
  },
  {
    key: 'errors',
    label: 'Failed',
    width: '110px',
    align: 'right',
    render: (row) =>
      row.errors > 0 ? (
        <span className="text-danger">
          {percent((row.errors / Math.max(1, row.requests)) * 100)}
        </span>
      ) : (
        <span className="text-faint">0%</span>
      ),
  },
];

export default function PerformancePage() {
  const [range] = useRange();
  const [model] = useQueryState('model', '');
  const { data, loading, error, reload } = useAnalytics<PerformanceAnalytics>(
    'analytics/performance',
    { range, model },
  );
  const t = data?.totals;
  const series = data?.series ?? [];
  const histogram = data?.ttft_histogram ?? [];
  const errorRate =
    t && t.requests > 0 ? ((t.errors + t.cancelled) / t.requests) * 100 : null;

  const engineSeries =
    data?.engine_series.available === true ? data.engine_series.series : null;
  const ttftLine = engineSeries?.ttft_seconds ?? [];
  const engineLabels = (ttftLine[0]?.points ?? []).map(([t]) =>
    new Date(t * 1000).toISOString(),
  );

  return (
    <>
      <ConsoleHeader
        title="Performance"
        description="How fast the platform answers, measured where the person is waiting."
      >
        <ModelPicker models={data?.available_models ?? []} />
        <RangePicker />
      </ConsoleHeader>

      <Section
        first
        title="Time to first token"
        hint="Measured by the orchestrator: it includes retrieval, reranking and every pre-pass, so it is the wait people actually experience."
        value={duration(t?.p50_ttft_ms ?? null)}
        delta={data?.deltas.p95_ttft_ms ?? null}
        deltaGoodWhenDown
        stamp="median across the period"
      >
        <ChartFrame
          height={230}
          loading={loading && !data}
          error={error}
          onRetry={reload}
          empty={!loading && series.every((p) => p.avg_ttft_ms == null)}
          emptyMessage="No latency telemetry in this period."
        >
          <AnalyticsChart
            labels={series.map((p) => p.bucket)}
            bucket={data?.range.bucket ?? 'day'}
            height={230}
            ariaLabel="Average time to first token over time"
            yFormat={(v) => (v == null ? '—' : duration(v))}
            series={[
              {
                name: 'Avg first token',
                area: true,
                data: series.map((p) => p.avg_ttft_ms),
                format: (v) => duration(v),
              },
            ]}
          />
        </ChartFrame>
      </Section>

      <Section title="Percentiles">
        <StatRow columns={6}>
          <Stat label="P50 first token" value={duration(t?.p50_ttft_ms ?? null)} />
          <Stat label="P95 first token" value={duration(t?.p95_ttft_ms ?? null)} />
          <Stat label="P99 first token" value={duration(t?.p99_ttft_ms ?? null)} />
          <Stat label="P50 response" value={duration(t?.p50_duration_ms ?? null)} />
          <Stat label="P95 response" value={duration(t?.p95_duration_ms ?? null)} />
          <Stat label="P99 response" value={duration(t?.p99_duration_ms ?? null)} />
        </StatRow>
      </Section>

      <Section
        title="Where the waits fall"
        hint="A mean hides the tail people complain about; this is the whole distribution."
      >
        <ChartFrame
          height={200}
          loading={loading && !data}
          empty={!loading && histogram.every((b) => b.count === 0)}
          emptyMessage="No latency telemetry in this period."
        >
          <AnalyticsChart
            labels={histogram.map((b) => b.label)}
            bucket="none"
            height={200}
            ariaLabel="Distribution of time to first token"
            series={[
              { name: 'Requests', bar: true, data: histogram.map((b) => b.count) },
            ]}
          />
        </ChartFrame>
      </Section>

      <Section title="Throughput and reliability">
        <StatRow columns={5}>
          <Stat
            label="Requests"
            value={compact(t?.requests ?? null)}
            sub={data ? `${compact(t?.users ?? null)} people` : undefined}
          />
          <Stat
            label="Tokens per second"
            value={
              t?.avg_tokens_per_second == null
                ? NOT_MEASURED
                : t.avg_tokens_per_second.toFixed(1)
            }
            sub="average per request"
          />
          <Stat label="Failed" value={compact(t?.errors ?? null)} />
          <Stat label="Cancelled" value={compact(t?.cancelled ?? null)} />
          <Stat
            label="Failure rate"
            value={percent(errorRate)}
            sub="failed or cancelled"
          />
        </StatRow>
      </Section>

      <Section title="By engine">
        {/* Capped: six rows of four numbers spread across 1900px is a table
            you read by tracking a finger across the screen. */}
        <div className="max-w-4xl">
          <AdminTable
            columns={ROUTE_COLUMNS}
            rows={data?.routes ?? []}
            rowKey={(row) => row.route}
            loading={loading && !data}
            minWidth={720}
            empty="No requests recorded in this period."
          />
        </div>
      </Section>

      <Section
        title="What the engines themselves report"
        hint="vLLM's own histograms, which start when the prompt reaches the engine. The gap against the figures above is the platform's own overhead."
      >
        {!data ? (
          <div className="h-56 animate-pulse rounded-xl bg-[var(--admin-control)]" />
        ) : data.engines.available ? (
          <>
            <StatRow columns={4}>
              {data.engines.engines
                .filter((e) => e.finished_requests != null)
                .map((e) => (
                  <Stat
                    key={e.service}
                    label={e.service}
                    value={durationFromSeconds(e.avg_ttft_seconds)}
                    sub={`${durationFromSeconds(e.avg_queue_seconds)} queued · ${compact(e.finished_requests)} served`}
                  />
                ))}
            </StatRow>
            {engineLabels.length > 0 && (
              <div className="mt-6">
                <AnalyticsChart
                  labels={engineLabels}
                  bucket="hour"
                  height={220}
                  ariaLabel="Engine time to first token over the last hours"
                  yFormat={(v) => (v == null ? '—' : `${v.toFixed(1)}s`)}
                  series={ttftLine.map((line) => ({
                    name: line.service,
                    data: line.points.map(([, v]) => v),
                    format: (v) => `${v.toFixed(2)}s`,
                  }))}
                />
              </div>
            )}
          </>
        ) : (
          <TelemetryUnavailable
            what="Engine performance"
            reason={data.engines.reason}
            source={data.engines.source}
          />
        )}
      </Section>
    </>
  );
}
