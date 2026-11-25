/**
 * The one place ECharts parts are registered (charts design v2 §2, §13).
 *
 * Imported only by components/EChart.tsx and lib/chartExport.ts, both of which
 * are loaded lazily, so ECharts never reaches the main bundle. Registering
 * only the chart types and components this app draws, through `echarts/core`,
 * keeps the chunk to what is used. v2 adds the dataZoom slider + inside
 * components (time series and windowed category lines) and the LabelLayout
 * feature (`labelLayout.moveOverlap` for line end labels).
 */

import * as echarts from 'echarts/core';
import { BarChart, FunnelChart, LineChart, PieChart, ScatterChart } from 'echarts/charts';
import {
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components';
import { LabelLayout } from 'echarts/features';
import { CanvasRenderer } from 'echarts/renderers';

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
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  LabelLayout,
  CanvasRenderer,
]);

export { echarts };

/** The instance methods the chat chart uses. */
export interface ChartInstance {
  resize: () => void;
  dispatchAction: (action: { type: string; [key: string]: unknown }) => void;
  getOption?: () => Record<string, unknown>;
  on?: (event: string, handler: (params: unknown) => void) => void;
  off?: (event: string, handler?: (params: unknown) => void) => void;
}
