// @vitest-environment jsdom
/**
 * Download PNG (charts design v2 §8.5, C16): the export draws the FULL chart
 * offscreen at 1200 px with a title and legend, clamps the pixel ratio under
 * ~16 MP, reports a failed canvas ('data:,') or a chart too tall to save, and
 * always disposes the detached instance. ECharts is faked: jsdom has no canvas.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  url: 'data:image/png;base64,AAAA',
  inits: [] as Array<{ width: number; height: number; devicePixelRatio: number }>,
  options: [] as Array<Record<string, unknown>>,
  disposed: 0,
}));

vi.mock('@/lib/echartsSetup', () => ({
  echarts: {
    init: (_el: unknown, _theme: unknown, opts: { width: number; height: number; devicePixelRatio: number }) => {
      h.inits.push(opts);
      return {
        setOption: (o: Record<string, unknown>) => h.options.push(o),
        getDataURL: () => h.url,
        dispose: () => {
          h.disposed += 1;
        },
      };
    },
  },
}));

import { exportChartPng, exportPixelRatio, EXPORT_WIDTH } from '@/lib/chartExport';
import { modelFromSpec } from '@/lib/chartModel';
import { fallbackPalette } from '@/lib/chartTheme';
import type { DataRow } from '@/lib/types';

beforeEach(() => {
  // jsdom cannot navigate to a download; the click itself is all we need.
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
  h.url = 'data:image/png;base64,AAAA';
  h.inits.length = 0;
  h.options.length = 0;
  h.disposed = 0;
});

function model(rows: DataRow[], type: 'bar' | 'horizontal_bar' | 'line' = 'horizontal_bar', keys = ['n']) {
  return modelFromSpec({ type, x_key: 'k', y_keys: keys, title: 'Records by owner', stacked: false }, rows).model!;
}

const OPTS = { context: 'inline' as const, surface: '#1e1e1e', title: 'Records by owner' };

describe('exportChartPng', () => {
  it('exports every row of a bar list at 1200 px, with a title, and disposes', async () => {
    const rows = Array.from({ length: 60 }, (_, i) => ({ k: `Owner ${i}`, n: i }));
    const r = await exportChartPng(model(rows), fallbackPalette('dark'), OPTS);
    expect(r).toEqual({ ok: true, fileName: 'records-by-owner.png' });
    expect(h.inits[0].width).toBe(EXPORT_WIDTH);
    const option = h.options[0];
    expect((option.title as { text: string }).text).toBe('Records by owner');
    expect(option.animation).toBe(false);
    expect(((option.yAxis as { data: string[] }).data)).toHaveLength(60);
    expect(option.dataZoom).toBeUndefined();
    expect(h.disposed).toBe(1);
  });

  it('reports a failed canvas instead of saving an empty file', async () => {
    h.url = 'data:,';
    const r = await exportChartPng(model([{ k: 'a', n: 1 }, { k: 'b', n: 2 }]), fallbackPalette('dark'), OPTS);
    expect(r).toEqual({ ok: false, reason: 'failed' });
    expect(h.disposed).toBe(1);
  });

  it('refuses a chart taller than 30,000 px', async () => {
    const long = 'word '.repeat(200).trim();
    const rows = Array.from({ length: 200 }, (_, i) => ({ k: `${long} ${i}`, n: i }));
    const r = await exportChartPng(model(rows), fallbackPalette('dark'), OPTS);
    expect(r).toEqual({ ok: false, reason: 'too-tall' });
    expect(h.inits).toHaveLength(0);
  });

  it('draws the full time range with no slider', async () => {
    const rows = Array.from({ length: 300 }, (_, i) => ({
      k: new Date(Date.UTC(2025, 0, 1) + i * 86_400_000).toISOString().slice(0, 10),
      n: i % 9,
    }));
    await exportChartPng(model(rows, 'bar'), fallbackPalette('light'), OPTS);
    expect(h.options[0].dataZoom).toBeUndefined();
  });
});

describe('exportPixelRatio', () => {
  it('is 2 for ordinary charts and shrinks to stay under ~16 MP', () => {
    expect(exportPixelRatio(1200, 600)).toBe(2);
    const tall = exportPixelRatio(1200, 20_000);
    expect(tall).toBeLessThan(1);
    expect(1200 * 20_000 * tall * tall).toBeLessThanOrEqual(16_000_001);
  });
});
