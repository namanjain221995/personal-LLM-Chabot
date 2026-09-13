'use client';

/**
 * Model analytics — the two halves of "how is the model doing".
 *
 * TOP HALF, from our own request telemetry: what each model was asked for and
 * what it delivered, per person, per route, over the selected window.
 *
 * BOTTOM HALF, live from each engine: what it is doing right now — queue,
 * KV cache, throughput, prefix-cache hit rate. That half is Prometheus, and
 * when Prometheus is not running the page says so rather than showing zeros.
 *
 * Written with the console on 2026-09-04 but never committed: the root
 * .gitignore's unanchored `models/` rule matched this route folder, so the
 * rail's Models link 404'd in every build made from git. The rule is anchored
 * now (2026-09-13). The Leaderboards "Models" tab ranks the same rows; this
 * page is the only one with the effort tiers and the live engine cards.
 */

import { AdminTable, type AdminColumn } from '@/components/admin/AdminTable';
import { ConsoleHeader, RangePicker, useRange } from '@/components/admin/analytics/filters';
import {
  ChartFrame,
  CoverageNote,
  HealthMark,
  InfraBlock,
  Num,
  Section,
  Stat,
  StatRow,
} from '@/components/admin/analytics/ui';
import { AnalyticsChart } from '@/components/admin/analytics/AnalyticsChart';
import {
  NOT_MEASURED,
  compact,
  duration,
  durationFromSeconds,
  exact,
  percent,
  ratio,
} from '@/components/admin/analytics/format';
import { useAnalytics } from '@/components/admin/analytics/useAnalytics';
import {
  EFFORT_LABEL,
  type Engine,
  type ModelsAnalytics,
  type ModelUsage,
} from '@/components/admin/analytics/types';

const COLUMNS: AdminColumn<ModelUsage>[] = [
  {
    key: 'model',
    label: 'Model',
    width: '26%',
    render: (row) => (
      <span className="min-w-0">
        <span className="block truncate text-[13px] font-medium text-ink">
          {row.model.split('/').pop() ?? row.model}
        </span>
        <span className="block truncate text-xs text-faint">{row.model}</span>
      </span>
    ),
  },
  {
    key: 'requests',
    label: 'Requests',
    width: '100px',
    align: 'right',
    render: (row) => <Num value={row.requests} />,
  },
  {
    key: 'share',
    label: 'Share',
    width: '80px',
    align: 'right',
    render: (row) => percent(row.share, 0),
  },
  {
    key: 'input',
    label: 'Input',
    width: '96px',
    align: 'right',
    hideBelowLg: true,
    render: (row) => (
      <span title={exact(row.input_tokens ?? null)}>
        {compact(row.input_tokens ?? null)}
      </span>
    ),
  },
  {
    key: 'output',
    label: 'Output',
    width: '96px',
    align: 'right',
    render: (row) => (
      <span title={exact(row.output_tokens)}>{compact(row.output_tokens)}</span>
    ),
  },
  {
    key: 'ttft',
    label: 'Avg TTFT',
    width: '104px',
    align: 'right',
    render: (row) => duration(row.avg_ttft_ms),
  },
  {
    key: 'p95',
    label: 'P95 TTFT',
    width: '104px',
    align: 'right',
    hideBelowLg: true,
    render: (row) => (
      <span className="text-muted">{duration(row.p95_ttft_ms ?? null)}</span>
    ),
  },
  {
    key: 'tps',
    label: 'Tokens/sec',
    width: '108px',
    align: 'right',
    render: (row) =>
      row.avg_tokens_per_second == null
        ? NOT_MEASURED
        : row.avg_tokens_per_second.toFixed(1),
  },
  {
    key: 'errors',
    label: 'Failed',
    width: '84px',
    align: 'right',
    render: (row) =>
      (row.errors ?? 0) > 0 ? (
        <span className="text-danger">{row.errors}</span>
      ) : (
        <span className="text-faint">0</span>
      ),
  },
];

/** One live engine. Every field may be absent; none of them defaults to 0. */
function EngineCard({ engine }: { engine: Engine }) {
  const busy = (engine.running ?? 0) > 0 || (engine.waiting ?? 0) > 0;
  return (
    <div className="rounded-xl border border-[var(--admin-separator)] p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium capitalize text-ink">
            {engine.service}
          </p>
          <p className="truncate text-xs text-faint" title={engine.model}>
            {engine.model.split('/').pop() ?? engine.model}
          </p>
        </div>
        <HealthMark ok={busy} okLabel="Serving" badLabel="Idle" />
      </div>
      <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3">
        <Stat label="Running" value={compact(engine.running)} />
        <Stat label="Queued" value={compact(engine.waiting)} />
        <Stat
          label="KV cache"
          value={percent(engine.kv_cache_percent, 1)}
        />
        <Stat
          label="Prefix cache hits"
          value={ratio(engine.prefix_cache_hit_rate)}
        />
        <Stat
          label="Avg first token"
          value={durationFromSeconds(engine.avg_ttft_seconds)}
        />
        <Stat
          label="Generated"
          value={
            engine.generation_tokens_total == null
              ? NOT_MEASURED
              : `${compact(engine.generation_tokens_total)} tok`
          }
          sub="since the engine started"
        />
      </dl>
    </div>
  );
}

export default function ModelAnalyticsPage() {
  const [range] = useRange();
  const { data, loading, error, reload } = useAnalytics<ModelsAnalytics>(
    'analytics/models',
    { range },
  );
  const models = data?.models ?? [];
  const effort = data?.effort ?? [];

  return (
    <>
      <ConsoleHeader
        title="Models"
        description="What each locally served model is carrying, and how fast it answers."
      >
        <RangePicker />
      </ConsoleHeader>

      {data && (
        <CoverageNote
          firstEvent={data.coverage.first_event}
          events={data.coverage.events}
          since={data.range.since}
        />
      )}

      <Section first title="Workload by model">
        <AdminTable
          columns={COLUMNS}
          rows={models}
          rowKey={(row) => row.model}
          loading={loading && !data}
          error={error}
          onRetry={reload}
          minWidth={1080}
          empty="No model telemetry for this period yet."
        />
      </Section>

      {models.length > 0 && (
        <Section
          title="Share of requests"
          hint="Which model the router actually sent the work to."
        >
          <ChartFrame height={220} loading={loading && !data}>
            <AnalyticsChart
              labels={models.map((m) => m.model.split('/').pop() ?? m.model)}
              bucket="none"
              height={220}
              ariaLabel="Requests by model"
              series={[
                { name: 'Requests', bar: true, data: models.map((m) => m.requests) },
              ]}
            />
          </ChartFrame>
        </Section>
      )}

      {effort.length > 0 && (
        <Section
          title="Effort tiers"
          hint="Fast, Think and Max are the same model at different reasoning budgets — this is what each costs in practice."
        >
          <StatRow columns={4}>
            {effort.map((e) => (
              <Stat
                key={e.effort}
                label={EFFORT_LABEL[e.effort] ?? e.effort}
                value={compact(e.requests)}
                sub={`${duration(e.avg_ttft_ms)} first token · ${duration(e.avg_duration_ms)} total`}
              />
            ))}
          </StatRow>
        </Section>
      )}

      <Section
        title="Engines right now"
        hint="Live from each vLLM engine through Prometheus — the state of the servers, not a stored history."
      >
        {/* InfraBlock, not a local ternary: a failed request would otherwise
            leave `data` null and this block pulsing as "loading" for good. */}
        <InfraBlock
          state={data?.engines}
          what="Live engine state"
          error={error}
          onRetry={reload}
        >
          {(block: { engines: Engine[] }) => (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {block.engines.map((engine) => (
                <EngineCard key={engine.service} engine={engine} />
              ))}
            </div>
          )}
        </InfraBlock>
      </Section>
    </>
  );
}
