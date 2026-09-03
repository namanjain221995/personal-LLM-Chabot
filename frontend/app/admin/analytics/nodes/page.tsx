'use client';

/**
 * Nodes — the machines this platform actually runs on.
 *
 * Monitoring only. There are no controls here: nothing on this page can
 * restart an engine, drain a node or change a model. Making a page that can
 * take the cluster down is a decision with its own review, and it is not this
 * one.
 *
 * Every value comes from Prometheus (node_exporter and the DGX GPU exporter,
 * both already scraped every 15 seconds). When the monitoring profile is not
 * running, the page says so — it never renders an unmeasured GPU as 0%.
 */

import { ConsoleHeader } from '@/components/admin/analytics/filters';
import {
  HealthMark,
  Meter,
  Section,
  Stat,
  StatRow,
  InfraBlock,
} from '@/components/admin/analytics/ui';
import {
  NOT_MEASURED,
  bytes,
  compact,
  durationFromSeconds,
  hertz,
  percent,
  rate,
  uptime,
} from '@/components/admin/analytics/format';
import { useAnalytics } from '@/components/admin/analytics/useAnalytics';
import type { Infrastructure, NodeState } from '@/components/admin/analytics/types';

function NodeCard({ node }: { node: NodeState }) {
  const memPercent =
    node.memory_total_bytes && node.memory_used_bytes != null
      ? (node.memory_used_bytes / node.memory_total_bytes) * 100
      : null;
  return (
    <div className="rounded-xl border border-[var(--admin-separator)] p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium text-ink">{node.node}</h3>
          <p className="truncate text-xs capitalize text-faint">
            {node.role || 'node'}
            {node.uptime_seconds != null && ` · up ${uptime(node.uptime_seconds)}`}
          </p>
        </div>
        <HealthMark ok={node.node_up} />
      </div>

      <div className="mt-5 space-y-4">
        <Meter label="GPU" value={node.gpu_utilization} caution={95} />
        <Meter label="CPU" value={node.cpu_percent} />
        <Meter label="Memory" value={memPercent} />
      </div>

      <dl className="mt-5 grid grid-cols-2 gap-x-4 gap-y-3 border-t border-[var(--admin-separator)] pt-4">
        <Stat
          label="GPU memory"
          value={bytes(node.gpu_memory_bytes)}
          sub={node.gpu_processes != null ? `${node.gpu_processes} processes` : undefined}
        />
        <Stat
          label="GPU temperature"
          value={
            node.gpu_temperature_c == null
              ? NOT_MEASURED
              : `${node.gpu_temperature_c.toFixed(0)}°C`
          }
          sub={node.gpu_throttled ? 'throttling' : undefined}
        />
        <Stat
          label="GPU power"
          value={
            node.gpu_power_w == null ? NOT_MEASURED : `${node.gpu_power_w.toFixed(0)} W`
          }
          sub={node.gpu_clock_hz != null ? hertz(node.gpu_clock_hz) : undefined}
        />
        <Stat
          label="System memory"
          value={bytes(node.memory_used_bytes)}
          sub={
            node.memory_total_bytes ? `of ${bytes(node.memory_total_bytes)}` : undefined
          }
        />
        <Stat
          label="Load average"
          value={node.load1 == null ? NOT_MEASURED : node.load1.toFixed(2)}
          sub="1 minute"
        />
        <Stat
          label="Network"
          value={rate(node.network_rx_bps)}
          sub={`${rate(node.network_tx_bps)} out`}
        />
      </dl>
    </div>
  );
}

export default function NodesPage() {
  const { data } = useAnalytics<Infrastructure>(
    'analytics/infrastructure',
    { hours: 6 },
  );

  const nodes = data?.nodes.available ? data.nodes.nodes : [];

  return (
    <>
      <ConsoleHeader
        title="Nodes"
        description="The machines serving this platform, read live from the monitoring stack."
      />

      <Section first title="Machines">
        <InfraBlock state={data?.nodes} what="Node telemetry" skeletonHeight={320}>
          {(block) => (
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              {block.nodes.map((node) => (
                <NodeCard key={node.node} node={node} />
              ))}
            </div>
          )}
        </InfraBlock>
      </Section>

      <Section
        title="Which engine runs where"
        hint="Each vLLM engine, the model it serves and the node it answers from."
      >
        <InfraBlock state={data?.engines} what="Engine placement">
          {(block) =>
            block.engines.length === 0 ? (
              <p className="rounded-lg border border-dashed border-[var(--admin-separator)] px-4 py-6 text-center text-xs text-faint">
                No engines are reporting.
              </p>
            ) : (
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {block.engines.map((engine) => (
                <div
                  key={engine.service}
                  className="rounded-xl border border-[var(--admin-separator)] p-4"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium capitalize text-ink">
                        {engine.service}
                      </p>
                      <p className="truncate text-xs text-faint" title={engine.model}>
                        {engine.model.split('/').pop() ?? engine.model}
                      </p>
                    </div>
                    <span className="shrink-0 rounded-md bg-[var(--admin-control)] px-1.5 py-0.5 text-[10px] text-muted">
                      {engine.node || 'unknown node'}
                    </span>
                  </div>
                  <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3">
                    <Stat label="Serving" value={compact(engine.running)} />
                    <Stat label="Queued" value={compact(engine.waiting)} />
                    <Stat label="KV cache" value={percent(engine.kv_cache_percent, 1)} />
                    <Stat
                      label="Queue wait"
                      value={durationFromSeconds(engine.avg_queue_seconds)}
                    />
                  </dl>
                </div>
              ))}
            </div>
            )
          }
        </InfraBlock>
      </Section>

      {nodes.length > 0 && (
        <Section title="Cluster totals">
          <StatRow columns={4}>
            <Stat label="Machines" value={String(nodes.length)} />
            <Stat
              label="Healthy"
              value={String(nodes.filter((n) => n.node_up).length)}
            />
            <Stat
              label="GPUs reporting"
              value={String(nodes.filter((n) => n.gpu_up).length)}
            />
            <Stat
              label="Total GPU power"
              value={
                nodes.every((n) => n.gpu_power_w == null)
                  ? NOT_MEASURED
                  : `${nodes
                      .reduce((sum, n) => sum + (n.gpu_power_w ?? 0), 0)
                      .toFixed(0)} W`
              }
            />
          </StatRow>
        </Section>
      )}
    </>
  );
}
