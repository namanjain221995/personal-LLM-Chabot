'use client';

/**
 * Chat charts (charts design v2, 2026-11-25): the `ChartFigure` shell and the
 * `ChartView` entry point for `meta.chart` + rows.
 *
 *   ChartView        spec + rows → ChartModel (lib/chartModel) → ChartFigure
 *   ChartFigure      measure width → choose form + plan (lib/chartLayout) →
 *                    one renderer + the shared HTML chrome
 *   renderers        ChartBarList (HTML) for every horizontal-bar form;
 *                    EChart (lazy canvas) for columns, lines, time series,
 *                    pie/donut, funnel, histogram, scatter and combos
 *   chrome           title, toolbar (Zoom out · Zoom in · Reset zoom · Table ·
 *                    PNG), HTML legend,
 *                    axis captions, notes, screen-reader summary, live
 *                    region, numbered key, Table
 *
 * The owner's rule (2026-11-25): the APPROPRIATE chart for the data, text
 * that never truncates or overlaps, long lists and series that scroll or zoom
 * instead of squeezing, an accessible palette in both themes, and real
 * interaction (tooltips, legend toggles, zoom, keyboard, PNG, table).
 *
 * Nothing here can execute backend-supplied configuration: options are built
 * by lib/chartOption.ts from the model, never from the spec.
 */

import {
  memo,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import dynamic from 'next/dynamic';
import type { ChartSpec, DataRow } from '@/lib/types';
import { type ChartModel, type ModelResult, cleanRows, modelFromSpec, PART_TO_WHOLE } from '@/lib/chartModel';
import { describeChart, layoutChart, type ChartLayout } from '@/lib/chartLayout';
import { buildOptionFromModel, type EChartsOption } from '@/lib/chartOption';
import {
  type ChartContext,
  fallbackPalette,
  resolvePalette,
  seriesColor,
  surfaceFor,
} from '@/lib/chartTheme';
import { chartFileName, formatDateByGrain, formatShare, formatValue } from '@/lib/chartFormat';
import { CHART_FONT, measurerFor, measureVersion, resetMeasureCache } from '@/lib/chartText';
import type { ChartInstance } from '@/lib/echartsSetup';
import { useTheme } from './Providers';
import { ChartErrorBoundary } from './ChartErrorBoundary';
import { DataTable } from './DataTable';
import { ChartBarList, type LiveMessage } from './chart/ChartBarList';
import { ChartLegend, type LegendItem } from './chart/ChartLegend';
import { ChartToolbar, type ToolbarZoom } from './chart/ChartToolbar';
import { useChartKeyboard, type ZoomApi } from './chart/useChartKeyboard';

const useIsoLayoutEffect = typeof window !== 'undefined' ? useLayoutEffect : useEffect;

// ECharts is not in the initial bundle and never renders on the server. The
// placeholder fills the box the figure has already reserved at the planned
// height, so nothing jumps when the chunk arrives (design §5.2).
const EChart = dynamic(() => import('./EChart'), {
  ssr: false,
  loading: () => (
    <div
      className="flex h-full w-full min-w-0 items-center justify-center rounded-md bg-surface-2/50"
      role="status"
      aria-label="Loading chart"
    >
      <span className="text-xs text-faint">Loading chart…</span>
    </div>
  ),
});

/** Width used before the figure has been measured (SSR, jsdom). */
const DEFAULT_WIDTH: Record<ChartContext, number> = { inline: 720, drawer: 640, mermaid: 680 };

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

/**
 * A media query as an external store (review A19): the FIRST render already
 * knows about a coarse pointer and reduced motion, so the first option handed
 * to ECharts is never animated for a reader who asked for no motion, and a
 * zoomed chart does not grow by the touch slider after first paint. The
 * server snapshot is false; charts never render on the server with data.
 */
function mediaStore(query: string) {
  return {
    subscribe(onChange: () => void) {
      if (typeof window === 'undefined' || !window.matchMedia) return () => undefined;
      const q = window.matchMedia(query);
      q.addEventListener?.('change', onChange);
      return () => q.removeEventListener?.('change', onChange);
    },
    get(): boolean {
      if (typeof window === 'undefined' || !window.matchMedia) return false;
      return Boolean(window.matchMedia(query)?.matches);
    },
  };
}
const COARSE = mediaStore('(pointer: coarse)');
const REDUCED = mediaStore('(prefers-reduced-motion: reduce)');
const serverFalse = () => false;

function useCoarsePointer(): boolean {
  return useSyncExternalStore(COARSE.subscribe, COARSE.get, serverFalse);
}
function useChartReducedMotion(): boolean {
  return useSyncExternalStore(REDUCED.subscribe, REDUCED.get, serverFalse);
}

/**
 * Re-plan when a web font finishes loading (design §4.1). One listener for
 * the whole page (review R6): it clears the shared width cache once and
 * bumps the version every figure reads, instead of every figure clearing
 * the cache for every other.
 */
const fontSubscribers = new Set<() => void>();
let fontsListening = false;
function subscribeFonts(onChange: () => void) {
  fontSubscribers.add(onChange);
  const fonts = typeof document !== 'undefined' ? document.fonts : undefined;
  if (fonts && !fontsListening) {
    fontsListening = true;
    fonts.load?.(CHART_FONT).then(
      () => undefined,
      () => undefined,
    );
    fonts.addEventListener?.('loadingdone', () => {
      resetMeasureCache();
      for (const notify of fontSubscribers) notify();
    });
  }
  return () => {
    fontSubscribers.delete(onChange);
  };
}
function useFontsVersion(): number {
  return useSyncExternalStore(subscribeFonts, measureVersion, measureVersion);
}

/** A live-region message; `seq` makes a repeat re-announce (review A13). */
type LiveEntry = LiveMessage & { seq: number };

export interface ChartFigureProps {
  model: ChartModel;
  context?: ChartContext;
}

/**
 * The figure around one chart. Memoised: a streaming answer re-renders its
 * row on every token while `meta` keeps its identity, so the chart does not
 * rebuild per token (design §8.7).
 */
export const ChartFigure = memo(function ChartFigure({ model, context = 'inline' }: ChartFigureProps) {
  const { theme } = useTheme();
  const reducedMotion = useChartReducedMotion();
  const coarse = useCoarsePointer();
  const fontsVersion = useFontsVersion();
  const uid = useId().replace(/:/g, '');
  const titleId = `chart-title-${uid}`;
  const summaryId = `chart-summary-${uid}`;
  const hintId = `chart-hint-${uid}`;
  const tableId = `chart-table-${uid}`;
  const plotRef = useRef<HTMLDivElement | null>(null);

  // Canvas colours must be concrete (a canvas cannot follow var()); start
  // from the literal fallbacks, then resolve the tokens per theme.
  const [palette, setPalette] = useState(() => fallbackPalette(theme));
  useEffect(() => {
    setPalette(resolvePalette(theme));
  }, [theme]);
  const surface = surfaceFor(theme === 'light' ? 'light' : 'dark', context);

  // ---- width (one ResizeObserver, throttled to a frame)
  const rootRef = useRef<HTMLElement | null>(null);
  const [width, setWidth] = useState(0);
  useIsoLayoutEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const read = () => {
      const w = Math.floor(el.getBoundingClientRect().width);
      if (w > 0) setWidth((prev) => (prev === w ? prev : w));
    };
    read();
    if (typeof ResizeObserver === 'undefined') return;
    let frame: number | null = null;
    const ro = new ResizeObserver(() => {
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        read();
      });
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, []);
  const planWidth = width > 0 ? width : DEFAULT_WIDTH[context];

  const layout = useMemo(
    () => layoutChart(model, { width: planWidth, context, coarse, measure: measurerFor() }),
    // fontsVersion: a web font that finished loading re-measures every label.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [model, planWidth, context, coarse, fontsVersion],
  );
  const layoutRef = useRef<ChartLayout>(layout);
  layoutRef.current = layout;

  // ---- state that survives rebuilds (C9)
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const hiddenRef = useRef(hidden);
  hiddenRef.current = hidden;
  const zoomRef = useRef<{ start: number; end: number } | null>(null);
  const [zoomed, setZoomed] = useState(false);
  const [zoomWin, setZoomWin] = useState<{ start: number; end: number } | null>(null);
  const resettingRef = useRef(false);
  const quietZoomRef = useRef(false);
  const instanceRef = useRef<ChartInstance | null>(null);
  const [tableOpen, setTableOpen] = useState(() => layout.tableOpen);
  const [live, setLive] = useState<LiveEntry | null>(null);
  const liveSeq = useRef(0);
  const [exporting, setExporting] = useState(false);
  const [exportMsg, setExportMsg] = useState('');
  const [kbdFocus, setKbdFocus] = useState(false);
  const [pointerIn, setPointerIn] = useState(false);

  // The option is rebuilt only when the drawn STRUCTURE changes; a pure width
  // change is a resize() inside EChart. Legend and zoom state are read from
  // refs, so they are carried into every rebuild instead of resetting.
  const option: EChartsOption | null = useMemo(() => {
    const l = layoutRef.current;
    const legendSelected: Record<string, boolean> = {};
    for (const id of hiddenRef.current) legendSelected[id] = false;
    return buildOptionFromModel(l.model, l, palette, {
      reducedMotion,
      legendSelected,
      zoom: zoomRef.current,
      surface,
      coarse,
      // Once a canvas exists, a rebuild must not replay the enter animation.
      firstDraw: instanceRef.current === null,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layout.structuralKey, model, palette, reducedMotion, surface, coarse]);

  const onLive = useCallback((msg: LiveMessage) => {
    liveSeq.current += 1;
    setLive({ ...msg, seq: liveSeq.current });
  }, []);

  // Review R12: a form switch (e.g. to the bar list) unmounts the canvas;
  // never dispatch to the disposed instance afterwards.
  useEffect(() => {
    if (layout.renderer !== 'echarts') instanceRef.current = null;
  }, [layout.renderer]);

  // ---- legend
  const pieLike = layout.form === 'pie' || layout.form === 'donut';
  const legendItems: LegendItem[] = useMemo(() => {
    const m = layout.model;
    if (pieLike) {
      const values = m.series[0]?.values ?? [];
      return m.x.cats.map((c, i) => {
        const v = values[i] ?? 0;
        return {
          id: c.id,
          label: c.label,
          color: seriesColor(palette, i),
          value: m.valueFlags.percentWhole ? `${formatValue(v)}%` : formatValue(v),
          share: m.valueFlags.percentWhole ? undefined : formatShare(v, m.valueFlags.total),
        };
      });
    }
    if (m.series.length < 2 || m.hideLegend) return [];
    return m.series.map((s) => ({ id: s.id, label: s.label, color: seriesColor(palette, s.slot) }));
  }, [layout.model, pieLike, palette]);

  const toggle = useCallback((id: string) => {
    instanceRef.current?.dispatchAction({ type: 'legendToggleSelect', name: id });
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const showAll = useCallback(() => {
    for (const id of hiddenRef.current) {
      instanceRef.current?.dispatchAction({ type: 'legendSelect', name: id });
    }
    setHidden(new Set());
  }, []);
  const hover = useCallback(
    (id: string | null) => {
      const chart = instanceRef.current;
      if (!chart) return;
      chart.dispatchAction({ type: 'downplay' });
      if (!id) return;
      if (pieLike) {
        const i = layoutRef.current.model.x.cats.findIndex((c) => c.id === id);
        if (i >= 0) chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: i });
      } else {
        chart.dispatchAction({ type: 'highlight', seriesId: id });
      }
    },
    [pieLike],
  );

  // ---- zoom
  const announceZoom = useRef<number | null>(null);
  const hasZoom = Boolean(layout.zoom);
  const zoomLimits = useCallback(() => {
    const l = layoutRef.current;
    const n = l.model.x.cats.length;
    const minW = Math.min(100, Math.max(2, (100 * 5) / Math.max(5, n)));
    const maxW = Math.max(minW, Math.min(100, l.zoom?.maxSpanPct ?? 100));
    return { minW, maxW };
  }, []);
  const zoomApi: ZoomApi | null = useMemo(() => {
    if (!hasZoom) return null;
    const get = () => zoomRef.current ?? readZoom(instanceRef.current) ?? { start: 0, end: 100 };
    const set = (start: number, end: number, quiet?: boolean) => {
      // ECharts emits `datazoom` synchronously inside dispatchAction, so the
      // flag covers exactly this one move.
      quietZoomRef.current = Boolean(quiet);
      instanceRef.current?.dispatchAction({ type: 'dataZoom', start, end });
      quietZoomRef.current = false;
    };
    return {
      get,
      set,
      zoomBy: (factor: number) => {
        const { start, end } = get();
        const { minW, maxW } = zoomLimits();
        const mid = (start + end) / 2;
        const w = Math.max(minW, Math.min(maxW, (end - start) * factor));
        const s = Math.max(0, Math.min(100 - w, mid - w / 2));
        set(s, s + w);
      },
      reset: () => {
        const l = layoutRef.current;
        if (!l.zoom) return;
        resettingRef.current = true;
        instanceRef.current?.dispatchAction({
          type: 'dataZoom',
          startValue: l.zoom.startValue,
          endValue: l.zoom.endValue,
        });
        zoomRef.current = null;
        setZoomed(false);
        setZoomWin(null);
      },
    };
  }, [hasZoom, zoomLimits]);

  const onEvents = useMemo(
    () => ({
      datazoom: () => {
        const z = readZoom(instanceRef.current);
        if (!z) return;
        if (resettingRef.current) {
          resettingRef.current = false;
          return;
        }
        zoomRef.current = z;
        setZoomed(true);
        setZoomWin(z);
        if (quietZoomRef.current) {
          // The keyboard moved the window to follow a point it just read.
          quietZoomRef.current = false;
          return;
        }
        if (announceZoom.current !== null) window.clearTimeout(announceZoom.current);
        announceZoom.current = window.setTimeout(() => {
          const range = zoomRangeText(layoutRef.current, z);
          if (range) onLive({ name: '', text: range });
        }, 400);
      },
    }),
    [onLive],
  );
  useEffect(
    () => () => {
      if (announceZoom.current !== null) window.clearTimeout(announceZoom.current);
    },
    [],
  );

  // Review A12 (WCAG 1.4.13): a tooltip shown by the pointer can be dismissed
  // with Esc even when focus is elsewhere (the composer, say).
  useEffect(() => {
    if (!pointerIn || typeof document === 'undefined') return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      instanceRef.current?.dispatchAction({ type: 'hideTip' });
      instanceRef.current?.dispatchAction({ type: 'downplay' });
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [pointerIn]);

  const onKeyDown = useChartKeyboard({ instance: instanceRef, layout, hidden, zoom: zoomApi, onLive });

  // ---- export (ECharts and the export module load only on this click)
  const download = useCallback(async () => {
    setExporting(true);
    setExportMsg('');
    // Review A3: the result is spoken, not only a failure shown.
    onLive({ name: '', text: 'Saving the chart as an image…' });
    try {
      const { exportChartPng } = await import('@/lib/chartExport');
      const result = await exportChartPng(model, palette, {
        context,
        surface,
        title: model.title || model.a11y?.title || 'Chart',
      });
      if (!result.ok) {
        setExportMsg(
          result.reason === 'too-tall'
            ? "Too tall to save as an image — use the table's CSV."
            : "Couldn't save the image — use the table's CSV.",
        );
        onLive({ name: '', text: 'The image was not saved.' });
      } else {
        onLive({ name: '', text: `Image saved as ${result.fileName}.` });
      }
    } catch {
      setExportMsg("Couldn't save the image — use the table's CSV.");
      onLive({ name: '', text: 'The image was not saved.' });
    } finally {
      setExporting(false);
    }
  }, [model, palette, context, surface, onLive]);

  const summary = useMemo(() => describeChart(layout), [layout]);
  const title = model.title || model.a11y?.title || '';
  const notes = layout.notes;
  const renderer = layout.renderer;
  const tableShown = tableOpen || renderer === 'table';
  const canExport = renderer === 'echarts' || renderer === 'barlist';
  const pieLikeOrParts = pieLike || layout.form === 'funnel';
  const hintText =
    renderer === 'barlist'
      ? 'Up and down arrows read the rows; Home and End jump.'
      : `${pieLikeOrParts ? 'Arrow keys read the parts.' : 'Arrow keys read the values.'}${
          layout.zoom ? ' Plus and minus zoom; 0 resets.' : ''
        }`;
  // Review V9: the measure's name ("Count", "Open cases") over a bar list,
  // where there is no value axis to carry it.
  const measureName =
    renderer === 'barlist'
      ? layout.model.axisCaptions?.y ??
        (layout.model.series.length === 1 && !/^(Value|Series \d+)$/.test(layout.model.series[0].label)
          ? layout.model.series[0].label
          : undefined)
      : undefined;
  const zoomTools: ToolbarZoom | null =
    zoomApi && layout.zoom
      ? (() => {
          const { minW, maxW } = zoomLimits();
          const z = zoomWin ?? readZoom(instanceRef.current);
          const w = z ? z.end - z.start : maxW;
          return {
            canZoomIn: w > minW + 0.01,
            canZoomOut: w < maxW - 0.01,
            zoomed,
            onZoomIn: () => zoomApi.zoomBy(1 / 1.5),
            onZoomOut: () => zoomApi.zoomBy(1.5),
            onReset: () => zoomApi.reset(),
          };
        })()
      : null;

  // ---- body
  let body: React.ReactNode = null;
  if (renderer === 'empty') {
    body = <p className="text-sm text-muted">{layout.emptyMessage}</p>;
  } else if (renderer === 'stat') {
    const s = layout.model.series[0];
    const v = s?.values[0] ?? null;
    body = (
      <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1" data-testid="chart-stat">
        <span dir="auto" className="break-words text-sm text-muted [overflow-wrap:anywhere]">
          {layout.model.x.cats[0]?.label}
          {s && s.label && s.label !== 'Value' ? ` · ${s.label}` : ''}
        </span>
        <span className="text-2xl font-semibold tabular-nums text-ink">
          {layout.model.valueFlags.percentWhole && v !== null ? `${formatValue(v)}%` : formatValue(v)}
        </span>
      </p>
    );
  } else if (renderer === 'barlist' && layout.barList) {
    body = (
      <ChartBarList
        model={layout.model}
        ordered={layout.barList.ordered}
        percentOfFirst={layout.barList.percentOfFirst}
        shareOfTotal={PART_TO_WHOLE.has(layout.model.requested)}
        cap={layout.barList.cap}
        hidden={hidden}
        reducedMotion={reducedMotion}
        labelledBy={titleId}
        describedBy={`${summaryId} ${hintId}`}
        onFocusVisible={setKbdFocus}
        onLive={onLive}
      />
    );
  } else if (renderer === 'echarts' && option) {
    const centre =
      layout.pie?.centreTotal && !layout.model.valueFlags.percentWhole ? (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center"
        >
          <span className="text-xs text-muted">Total</span>
          <span className="text-lg font-semibold tabular-nums text-ink">
            {formatValue(layout.model.valueFlags.total)}
          </span>
        </div>
      ) : null;
    const plot = (
      <div
        ref={plotRef}
        className="chart-plot relative min-w-0 shrink-0 rounded-md"
        style={{ width: pieLike ? layout.width : undefined, maxWidth: '100%' }}
        tabIndex={0}
        role="group"
        aria-roledescription="chart"
        aria-labelledby={titleId}
        aria-describedby={`${summaryId} ${hintId}`}
        data-testid="chart-plot"
        onKeyDown={onKeyDown}
        onFocus={(e) => {
          let visible = true;
          try {
            visible = e.currentTarget.matches(':focus-visible');
          } catch {
            visible = true;
          }
          setKbdFocus(visible);
        }}
        onBlur={() => {
          setKbdFocus(false);
          instanceRef.current?.dispatchAction({ type: 'hideTip' });
        }}
        onPointerEnter={() => setPointerIn(true)}
        onPointerLeave={() => setPointerIn(false)}
      >
        {/* The reserved box: the planned height, before the chunk loads. */}
        <div style={{ height: layout.height }} className="w-full" data-testid="chart-reserved">
          <EChart
            option={option}
            height={layout.height}
            interactive
            onReady={(instance) => {
              instanceRef.current = instance;
            }}
            onEvents={onEvents}
          />
        </div>
        {centre}
      </div>
    );
    body = pieLike ? (
      <div className="chart-pie-body" data-legend={layout.pie?.legend}>
        {plot}
        <ChartLegend
          items={legendItems}
          hidden={hidden}
          onToggle={toggle}
          onShowAll={showAll}
          onHover={hover}
          layout="column"
          label="Parts"
        />
      </div>
    ) : (
      plot
    );
  }

  return (
    <figure
      ref={rootRef as React.RefObject<HTMLElement>}
      className="chart-figure m-0"
      data-form={layout.form}
      data-context={context}
      // The title sits in a header row with the toolbar, so it cannot be a
      // direct-child <figcaption>; it names the figure by id instead.
      aria-labelledby={titleId}
    >
      <div className="mb-2 flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
        <div
          id={titleId}
          className="min-w-0 flex-1 break-words text-sm font-medium text-ink [overflow-wrap:anywhere]"
        >
          {title ? <span dir="auto">{title}</span> : <span className="text-muted">Chart</span>}
        </div>
        <ChartToolbar
          tableOpen={tableShown}
          tableId={tableId}
          onToggleTable={() => setTableOpen(!tableShown)}
          showTable={renderer !== 'table'}
          canExport={canExport}
          exporting={exporting}
          onExport={download}
          zoom={zoomTools}
        />
      </div>

      <p id={summaryId} className="sr-only">
        {summary.map((p, i) =>
          typeof p === 'string' ? <span key={i}>{p}</span> : <bdi key={i}>{p.name}</bdi>,
        )}
        {model.a11y?.description ? ` ${model.a11y.description}` : ''}
      </p>

      {notes.length > 0 && (
        <ul className="mb-2 list-none space-y-0.5 p-0 text-xs text-muted">
          {notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}

      {!pieLike && legendItems.length > 0 && (renderer === 'echarts' || renderer === 'barlist') && (
        <div className="mb-1.5">
          <ChartLegend
            items={legendItems}
            hidden={hidden}
            onToggle={toggle}
            onShowAll={showAll}
            onHover={renderer === 'echarts' ? hover : undefined}
          />
        </div>
      )}

      {layout.model.axisCaptions?.y && renderer === 'echarts' && (
        <p dir="auto" className="mb-1 break-words text-xs text-muted [overflow-wrap:anywhere]">
          {layout.model.axisCaptions.y}
        </p>
      )}
      {measureName && (
        <p dir="auto" className="chart-measure mb-1 break-words text-xs text-muted [overflow-wrap:anywhere]">
          {measureName}
        </p>
      )}

      {/* The hint floats under the plot on keyboard focus (review A20): it
          is an overlay like a tooltip, so nothing below it moves. */}
      <div className="chart-body relative">
        {body}
        {(renderer === 'echarts' || renderer === 'barlist') && (
          <p id={hintId} className={kbdFocus ? 'chart-hint chart-hint-pop' : 'sr-only'}>
            {hintText}
          </p>
        )}
      </div>

      {layout.model.axisCaptions?.x && renderer === 'echarts' && (
        <p dir="auto" className="mt-1 break-words text-center text-xs text-muted [overflow-wrap:anywhere]">
          {layout.model.axisCaptions.x}
        </p>
      )}

      {layout.numbered && (
        <ol
          className="mt-2 grid list-none gap-x-4 gap-y-0.5 p-0 text-xs text-muted sm:grid-cols-2"
          aria-label="Key"
        >
          {layout.model.x.cats.map((c, i) => (
            <li key={c.id} className="min-w-0 break-words [overflow-wrap:anywhere]">
              <span className="tabular-nums text-ink">{i + 1}</span> · <span dir="auto">{c.label}</span>
            </li>
          ))}
        </ol>
      )}

      <div aria-live="polite" className="sr-only" data-testid="chart-live">
        {live ? (
          // A new key per message: the node is replaced, so a repeat (End
          // pressed at the end) is announced again (review A13).
          <span key={live.seq}>
            {live.name ? (
              <>
                <bdi>{live.name}</bdi>:{' '}
              </>
            ) : null}
            {live.text}
          </span>
        ) : null}
      </div>

      {exportMsg && (
        <p role="status" className="mt-1 text-xs text-danger">
          {exportMsg}
        </p>
      )}

      <div id={tableId} hidden={!tableShown} className="mt-2">
        {tableShown && (
          <DataTable rows={layout.model.tableRows} csvName={chartFileName(title || 'chart', 'csv')} />
        )}
      </div>
    </figure>
  );
});

function readZoom(chart: ChartInstance | null): { start: number; end: number } | null {
  try {
    const opt = chart?.getOption?.() as
      | { dataZoom?: Array<{ start?: number; end?: number }> }
      | undefined;
    const dz = opt?.dataZoom?.[0];
    if (!dz || typeof dz.start !== 'number' || typeof dz.end !== 'number') return null;
    return { start: dz.start, end: dz.end };
  } catch {
    return null;
  }
}

function zoomRangeText(layout: ChartLayout, z: { start: number; end: number }): string {
  const { model } = layout;
  const n = model.x.cats.length;
  if (n === 0) return '';
  if (layout.form === 'timeLine' || layout.form === 'timeBars') {
    const min = layout.timeAxis?.min ?? 0;
    const max = layout.timeAxis?.max ?? 0;
    const at = (p: number) => min + ((max - min) * p) / 100;
    const grain = model.x.grain ?? 'day';
    return `Showing ${formatDateByGrain(at(z.start), grain)} to ${formatDateByGrain(at(z.end), grain)}`;
  }
  const a = Math.round(((n - 1) * z.start) / 100);
  const b = Math.round(((n - 1) * z.end) / 100);
  return `Showing ${model.x.cats[a]?.label ?? ''} to ${model.x.cats[b]?.label ?? ''}`;
}

/**
 * A `meta.chart` chart: the spec + rows entry point. The props are what they
 * always were, plus `context` (where it sits: the answer, the proof drawer).
 */
export function ChartView(props: {
  spec: ChartSpec;
  data: DataRow[];
  context?: ChartContext;
  /** @deprecated v1 sizing; the figure now reserves its planned height. */
  minHeight?: number;
}) {
  // Review R1: the app has no other error boundary, so EVERYTHING a chart
  // does — building the model included — happens inside this one.
  return (
    <ChartErrorBoundary>
      <ChartViewBody {...props} />
    </ChartErrorBoundary>
  );
}

function ChartViewBody({
  spec,
  data,
  context = 'inline',
}: {
  spec: ChartSpec;
  data: DataRow[];
  context?: ChartContext;
}) {
  const result: ModelResult = useMemo(() => {
    try {
      return modelFromSpec(spec, data);
    } catch {
      return { problem: 'unsupported-type' };
    }
  }, [spec, data]);
  if (!result.model) {
    const rows = cleanRows(data);
    const title = typeof spec?.title === 'string' ? spec.title : '';
    return (
      <figure className="chart-figure m-0">
        {title ? <figcaption className="mb-2 text-sm font-medium text-ink">{title}</figcaption> : null}
        <ChartUnavailable reason={MESSAGES[result.problem] ?? MESSAGES['unsupported-type']} />
        {/* Design §3 rule 0: the message, and the figures as a table. */}
        {rows.length > 0 && (
          <div className="mt-2">
            <DataTable rows={rows} csvName={chartFileName(title || 'chart', 'csv')} />
          </div>
        )}
      </figure>
    );
  }
  return <ChartFigure model={result.model} context={context} />;
}
