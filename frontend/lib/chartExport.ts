/**
 * Download PNG for chat charts (charts design v2 §8.5, C16).
 *
 * Loaded ONLY with `await import('@/lib/chartExport')` from the figure's
 * Download button. It imports lib/echartsSetup, so a bar-list answer (which
 * draws no canvas on screen) downloads ECharts only when the reader asks for
 * an image, and ECharts stays out of the main bundle either way.
 *
 * The export is always the FULL chart: every row, the whole time range, no
 * slider, with a canvas title and legend whose text is pre-broken at the
 * export width (a canvas legend item never wraps by itself). The device pixel
 * ratio is clamped so the canvas stays under ~16 MP (iOS Safari's limit), and
 * a failed canvas (`getDataURL` → 'data:,') is reported instead of saving an
 * empty file. The detached instance is always disposed.
 */

import { echarts } from './echartsSetup';
import { buildExportOption } from './chartOption';
import type { ChartModel } from './chartModel';
import { layoutChart } from './chartLayout';
import type { ChartPalette, ChartContext } from './chartTheme';
import { chartFileName } from './chartFormat';
import { measurerFor } from './chartText';

export const EXPORT_WIDTH = 1200;
export const MAX_EXPORT_HEIGHT = 30_000;
const MAX_PIXELS = 16_000_000;

export type ExportResult = { ok: true; fileName: string } | { ok: false; reason: 'too-tall' | 'failed' };

/** Pixel ratio that keeps a width × height canvas under the pixel budget. */
export function exportPixelRatio(width: number, height: number): number {
  return Math.min(2, Math.sqrt(MAX_PIXELS / Math.max(1, width * height)));
}

export async function exportChartPng(
  model: ChartModel,
  palette: ChartPalette,
  opts: { context: ChartContext; surface: string; title: string },
): Promise<ExportResult> {
  const measure = measurerFor();
  // Planned for the image (review V1/R3): no zoom window, numbered ticks + key instead.
  const layout = layoutChart(model, { width: EXPORT_WIDTH, context: opts.context, measure, forExport: true });
  const built = buildExportOption(layout.model, layout, palette, {
    title: opts.title,
    measure,
    surface: opts.surface,
  });
  if (!built) return { ok: false, reason: 'failed' };
  if (built.height > MAX_EXPORT_HEIGHT) return { ok: false, reason: 'too-tall' };
  const host = document.createElement('div');
  host.style.cssText = `position:fixed;left:-99999px;top:0;width:${EXPORT_WIDTH}px;height:${built.height}px;`;
  document.body.appendChild(host);
  let chart: ReturnType<typeof echarts.init> | null = null;
  try {
    chart = echarts.init(host, undefined, {
      renderer: 'canvas',
      width: EXPORT_WIDTH,
      height: built.height,
      devicePixelRatio: exportPixelRatio(EXPORT_WIDTH, built.height),
    });
    chart.setOption(built.option as never, { notMerge: true });
    const url = chart.getDataURL({ type: 'png', backgroundColor: opts.surface, pixelRatio: exportPixelRatio(EXPORT_WIDTH, built.height) });
    if (!url || url === 'data:,') return { ok: false, reason: 'failed' };
    const fileName = chartFileName(opts.title || 'chart');
    const a = document.createElement('a');
    a.href = url;
    a.download = fileName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    return { ok: true, fileName };
  } catch {
    return { ok: false, reason: 'failed' };
  } finally {
    chart?.dispose();
    host.remove();
  }
}
