'use client';

/**
 * GPU analytics — utilisation, memory, temperature and power over time.
 *
 * One chart per measurement with one line per node, so "is one node
 * overloaded" is answerable at a glance instead of by comparing two pages.
 * The window is short by design (hours, not days): GPU telemetry is scraped
 * every 15 seconds and a month of it at that resolution is noise, not
 * insight.
 */

import { AnalyticsChart, type Series } from '@/components/admin/analytics/AnalyticsChart';
import { ConsoleHeader, useQueryState } from '@/components/admin/analytics/filters';
import { AdminSelect } from '@/components/admin/controls';
import {
  ChartFrame,
  Meter,
  Section,
  Stat,
  InfraBlock,
} from '@/components/admin/analytics/ui';
import { NOT_MEASURED, bytes, hertz } from '@/components/admin/analytics/format';
import { useAnalytics } from '@/components/admin/analytics/useAnalytics';
import type { Infrastructure } from '@/components/admin/analytics/types';

const WINDOWS = [
  { value: '1', label: 'Last hour' },
  { value: '6', label: 'Last 6 hours' },
  { value: '24', label: 'Last 24 hours' },
  { value: '48', label: 'Last 48 hours' },
];

const CHARTS: {
  key: string;
  title: string;
  hint: string;
  unit: (value: number) => string;
}[] = [
  {
    key: 'utilization',
    title: 'GPU utilisation',
    hint: 'Percentage of time the GPU was executing work.',
    unit: (v) => `${v.toFixed(0)}%`,
  },
  {
    key: 'memory',
    title: 'GPU memory',
    hint: 'Allocated device memory — model weights plus the KV cache.',
    unit: (v) => bytes(v),
  },
  {
    key: 'temperature',
    title: 'GPU temperature',
    hint: 'Sustained high temperature is what precedes clock throttling.',
    unit: (v) => `${v.toFixed(0)}°C`,
  },
  {
    key: 'power',
    title: 'GPU power draw',
    hint: 'Instantaneous board power.',
    unit: (v) => `${v.toFixed(0)} W`,
  },
];

export default function GpuPage() {
  const [hours, setHours] = useQueryState('hours', '6');
  const { data, loading, error, reload } = useAnalytics<Infrastructure>(
    'analytics/infrastructure',
    { hours: Number(hours) || 6 },
  );

  const nodes = data?.nodes.available ? data.nodes.nodes : [];
  const gpuNodes = nodes.filter((n) => n.gpu_present);
  const gpuSeries = data?.gpu_series.available ? data.gpu_series.series : null;

  return (
    <>
      <ConsoleHeader
        title="GPU"
        description="Accelerator telemetry from every node in the cluster."
      >
        <AdminSelect
          value={hours}
          onChange={setHours}
          label="Time range"
          options={WINDOWS}
        />
      </ConsoleHeader>

      <Section first title="Right now">
        <InfraBlock state={data?.nodes} what="GPU telemetry" skeletonHeight={260}>
          {() =>
            gpuNodes.length === 0 ? (
            <p className="rounded-lg border border-dashed border-[var(--admin-separator)] px-4 py-6 text-center text-xs text-faint">
              No GPU is reporting on any node.
            </p>
          ) : (
            <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
              {gpuNodes.map((node) => (
                <div
                  key={node.node}
                  className="rounded-xl border border-[var(--admin-separator)] p-5"
                >
                  <div className="flex items-baseline justify-between">
                    <h3 className="text-sm font-medium text-ink">{node.node}</h3>
                    <span className="text-xs capitalize text-faint">{node.role}</span>
                  </div>
                  <div className="mt-4">
                    <Meter label="Utilisation" value={node.gpu_utilization} caution={95} />
                  </div>
                  <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3">
                    <Stat label="Memory" value={bytes(node.gpu_memory_bytes)} />
                    <Stat
                      label="Temperature"
                      value={
                        node.gpu_temperature_c == null
                          ? NOT_MEASURED
                          : `${node.gpu_temperature_c.toFixed(0)}°C`
                      }
                      sub={node.gpu_throttled ? 'throttling' : 'not throttling'}
                    />
                    <Stat
                      label="Power"
                      value={
                        node.gpu_power_w == null
                          ? NOT_MEASURED
                          : `${node.gpu_power_w.toFixed(0)} W`
                      }
                    />
                    <Stat label="Clock" value={hertz(node.gpu_clock_hz)} />
                  </dl>
                </div>
              ))}
            </div>
            )
          }
        </InfraBlock>
      </Section>

      {data && !gpuSeries && (
        <Section title="History">
          <InfraBlock state={data.gpu_series} what="GPU history">
            {() => null}
          </InfraBlock>
        </Section>
      )}

      {gpuSeries &&
        CHARTS.map((chart) => {
          const lines = gpuSeries[chart.key] ?? [];
          const labels = (lines[0]?.points ?? []).map(([t]) =>
            new Date(t * 1000).toISOString(),
          );
          const series: Series[] = lines.map((line) => ({
            name: line.node,
            area: lines.length === 1,
            data: line.points.map(([, v]) => v),
            format: chart.unit,
          }));
          return (
            <Section key={chart.key} title={chart.title} hint={chart.hint}>
              <ChartFrame
                height={200}
                loading={loading && !data}
                error={error}
                onRetry={reload}
                empty={labels.length === 0}
                emptyMessage="No samples in this window."
              >
                <AnalyticsChart
                  labels={labels}
                  bucket="hour"
                  height={200}
                  ariaLabel={`${chart.title} over time, by node`}
                  yFormat={(v) => (v == null ? '—' : chart.unit(v))}
                  series={series}
                />
              </ChartFrame>
            </Section>
          );
        })}
    </>
  );
}
