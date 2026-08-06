'use client';

/**
 * The Apache ECharts renderer, isolated behind a dynamic import.
 *
 * Two reasons this is its own file:
 *
 *  1. Bundle. `import 'echarts'` pulls the whole library. Registering only
 *     the five chart types and four components this app draws, through
 *     `echarts/core`, keeps the payload to what is actually used — and
 *     because ChartView loads this module with `next/dynamic` + `ssr:
 *     false`, none of it is in the initial chunk. A conversation with no
 *     chart never downloads ECharts at all.
 *
 *  2. SSR. ECharts needs a DOM to measure and a canvas to draw on; it
 *     cannot render on the server.
 *
 * `option` is built entirely by lib/chartOption.ts. Nothing from the
 * backend reaches this component except through that adapter.
 */

import { useEffect, useRef } from 'react';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import {
  BarChart,
  FunnelChart,
  LineChart,
  PieChart,
  ScatterChart,
} from 'echarts/charts';
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsOption } from '@/lib/chartOption';

/** The instance handle echarts-for-react hands back — resize is all we need. */
type ChartInstance = { resize: () => void };

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  FunnelChart,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  CanvasRenderer,
]);

export default function EChart({
  option,
  height = 300,
  ariaLabel,
}: {
  option: EChartsOption;
  height?: number;
  ariaLabel?: string;
}) {
  const wrapper = useRef<HTMLDivElement | null>(null);
  const chart = useRef<ChartInstance | null>(null);

  // echarts-for-react only listens to window resize. The proof drawer
  // changes width without the window changing at all, which left charts
  // drawn at their old size until something else forced a reflow.
  useEffect(() => {
    const el = wrapper.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => chart.current?.resize());
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={wrapper}
      className="w-full min-w-0"
      style={{ height }}
      role="img"
      aria-label={ariaLabel}
    >
      <ReactEChartsCore
        echarts={echarts}
        option={option}
        // The option is rebuilt whole on every theme or data change, so
        // merging into the previous one would leave stale series behind.
        notMerge
        lazyUpdate
        style={{ height: '100%', width: '100%' }}
        opts={{ renderer: 'canvas' }}
        onChartReady={(instance: ChartInstance) => {
          chart.current = instance;
        }}
      />
    </div>
  );
}
