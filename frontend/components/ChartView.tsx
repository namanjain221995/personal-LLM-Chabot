'use client';

/**
 * Proof-drawer Chart section (§9), rendered with Apache ECharts.
 *
 * The component's interface is unchanged — `<ChartView spec data />` — and
 * so is the contract it renders: a validated `ChartSpec` from `meta.chart`
 * plus row objects. Only the renderer changed.
 *
 * The split is deliberate:
 *
 *   ChartView (here)   validate → resolve theme → build option → fall back
 *   lib/chartOption    the trusted ChartSpec → EChartsOption adapter
 *   EChart             the dynamically-imported ECharts canvas
 *
 * All the logic worth testing lives in lib/chartOption.ts and
 * lib/chartTheme.ts, which are pure TypeScript and run under the existing
 * node-environment vitest setup. This file is thin on purpose.
 *
 * Nothing here can execute backend-supplied configuration: the option is
 * constructed by the adapter from a fixed set of spec fields, and this
 * component never passes `spec` to ECharts.
 */

import { useEffect, useMemo, useState } from 'react';
import dynamic from 'next/dynamic';
import type { ChartSpec, DataRow } from '@/lib/types';
import { buildChartOption, validateChart } from '@/lib/chartOption';
import { fallbackPalette, resolvePalette } from '@/lib/chartTheme';
import { useTheme } from './Providers';
import { ChartErrorBoundary } from './ChartErrorBoundary';

// ECharts is not in the initial bundle and never renders on the server.
const EChart = dynamic(() => import('./EChart'), {
  ssr: false,
  // A VISIBLE loading state. The placeholder used to be an empty 300px div,
  // which on a dark surface is indistinguishable from a chart that failed to
  // draw — screenshots of "blank charts" were mostly this while the ECharts
  // chunk downloaded.
  loading: () => (
    <div
      className="flex h-[300px] w-full min-w-0 items-center justify-center rounded-md bg-surface-2/50"
      role="status"
      aria-label="Loading chart"
    >
      <span className="text-xs text-faint">Loading chart…</span>
    </div>
  ),
});

function ChartUnavailable({ reason }: { reason: string }) {
  return <p className="text-sm text-muted">{reason}</p>;
}

const MESSAGES: Record<string, string> = {
  'no-data': 'No rows to chart — the result is in the Data tab.',
  'unsupported-type': 'This chart type is not supported here. The data is in the Data tab.',
  'missing-x-column': 'The chart refers to a column this result does not have.',
  'missing-y-column': 'The chart refers to a column this result does not have.',
  'scatter-needs-numeric-x': 'A scatter chart needs two numeric columns.',
  'no-numeric-values': 'Nothing numeric to plot — the data is in the Data tab.',
  'part-to-whole-needs-positive-values':
    'A pie or donut needs positive values. The data is in the Data tab.',
};

function ChartCanvas({ spec, data }: { spec: ChartSpec; data: DataRow[] }) {
  const { theme } = useTheme();
  // Resolving CSS custom properties needs a DOM. Start from the literal
  // fallbacks so the first paint (and SSR) is correct, then swap in the
  // real token values — and re-resolve whenever the theme changes, because
  // a canvas cannot follow a `var()` the way an SVG attribute could.
  const [palette, setPalette] = useState(() => fallbackPalette(theme));
  useEffect(() => {
    setPalette(resolvePalette(theme));
  }, [theme]);

  const problem = validateChart(spec, data);
  const option = useMemo(
    () => (problem ? null : buildChartOption(spec, data, palette)),
    [spec, data, palette, problem],
  );

  if (problem) {
    return <ChartUnavailable reason={MESSAGES[problem] ?? MESSAGES['unsupported-type']} />;
  }
  if (!option) {
    return <ChartUnavailable reason="Chart could not be displayed. The figures are in the Data tab." />;
  }

  // A horizontal bar GROWS with its categories instead of squeezing them into
  // a fixed 300px. Thirty recruiters in 300px is ten pixels a bar with the
  // labels overlapping into a grey smear; the same thirty at 22px each is a
  // readable ranking you scroll. Bounded so one runaway result cannot produce
  // a page-length image.
  const height =
    spec.type === 'horizontal_bar'
      ? Math.min(1400, Math.max(300, data.length * 26 + 96))
      : 300;
  return <EChart option={option} height={height} ariaLabel={spec.title || 'Chart'} />;
}

export function ChartView({ spec, data }: { spec: ChartSpec; data: DataRow[] }) {
  return (
    <figure>
      {spec.title ? (
        <figcaption className="mb-2 text-sm font-medium text-ink">{spec.title}</figcaption>
      ) : null}
      <ChartErrorBoundary>
        <ChartCanvas spec={spec} data={data} />
      </ChartErrorBoundary>
    </figure>
  );
}
