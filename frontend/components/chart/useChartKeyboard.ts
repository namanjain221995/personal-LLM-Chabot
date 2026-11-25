'use client';

/**
 * Keyboard reading for an ECharts plot (charts design v2 §8.4, C15).
 *
 * The plot is a focusable `role="group"`. Keys:
 *   ←/→          step through columns, line points and time points
 *   ↑/↓          step through pie / funnel parts (←/→ work there too)
 *   Home / End   first / last
 *   + / − / 0    zoom in / out / back to the first window (when zoom is on)
 *   Esc          hide the tooltip
 * Each move dispatches `showTip` + `highlight`, shifts the zoom window when
 * the point is outside it, and writes "<name>: values" to a polite live
 * region. Tab, Space and PageUp/PageDown are never captured.
 *
 * Honest scope: screen readers in browse mode do not pass arrow keys to a
 * group, so this serves sighted keyboard users and screen-reader users in
 * focus mode; everyone else has the summary, the legend and the Table.
 */

import { useCallback, useRef } from 'react';
import type { ChartInstance } from '@/lib/echartsSetup';
import type { ChartLayout } from '@/lib/chartLayout';
import { formatShare, formatValue } from '@/lib/chartFormat';
import type { LiveMessage } from './ChartBarList';

export interface ZoomApi {
  /** Current window in percent of the full range. */
  get: () => { start: number; end: number };
  /**
   * Move the window. `quiet`: a keyboard step that only follows the point
   * the reader is on, whose range must not overwrite that point's
   * announcement (review A13).
   */
  set: (start: number, end: number, quiet?: boolean) => void;
  /** Widen (factor > 1) or narrow the window around its centre, within its limits. */
  zoomBy: (factor: number) => void;
  reset: () => void;
}

const CARTESIAN = new Set(['columns', 'timeColumns', 'catLine', 'timeLine', 'timeBars', 'histogram', 'combo', 'scatter']);

export function useChartKeyboard({
  instance,
  layout,
  hidden,
  zoom,
  onLive,
}: {
  instance: React.MutableRefObject<ChartInstance | null>;
  layout: ChartLayout;
  hidden: ReadonlySet<string>;
  zoom: ZoomApi | null;
  onLive: (msg: LiveMessage) => void;
}) {
  const index = useRef(-1);

  const dataIndexOf = useCallback(
    (i: number) => {
      if (layout.form !== 'timeLine') return i;
      // Each gap inserted one null point before the categories after it.
      return i + layout.model.x.breaks.filter((b) => b < i).length;
    },
    [layout],
  );

  const announce = useCallback(
    (i: number) => {
      const { model, form } = layout;
      const cat = model.x.cats[i];
      if (!cat) return;
      if (form === 'pie' || form === 'donut' || form === 'funnel') {
        if (hidden.has(cat.id)) return;
        const v = model.series[0]?.values[i] ?? null;
        const base = form === 'funnel' ? (model.series[0]?.values[0] ?? 0) : model.valueFlags.total;
        const share = v !== null && base ? ` (${formatShare(v, base)})` : '';
        const value = model.valueFlags.percentWhole && v !== null ? `${formatValue(v)}%` : `${formatValue(v)}${share}`;
        onLive({ name: cat.label, text: value });
        return;
      }
      const visible = model.series.filter((s) => !hidden.has(s.id));
      if (visible.length === 0) {
        // Review A14: nothing drawn, so nothing to read but that.
        onLive({ name: '', text: 'All series hidden. Use the legend to show one.' });
        return;
      }
      const multi = model.series.length > 1;
      const text = visible
        .map((s) => `${multi ? `${s.label} ` : ''}${formatValue(s.values[i] ?? null)}`)
        .join(', ');
      onLive({ name: cat.label, text: cat.present === false ? `no row for ${cat.label}` : text });
    },
    [layout, hidden, onLive],
  );

  const keepInWindow = useCallback(
    (i: number) => {
      if (!zoom || !layout.zoom) return;
      const n = layout.model.x.cats.length;
      let p: number;
      if (layout.form === 'timeLine' || layout.form === 'timeBars') {
        const min = layout.timeAxis?.min ?? 0;
        const max = layout.timeAxis?.max ?? 1;
        const t = layout.model.x.cats[i]?.t ?? min;
        p = max > min ? ((t - min) / (max - min)) * 100 : 0;
      } else {
        p = n > 1 ? (i / (n - 1)) * 100 : 0;
      }
      const { start, end } = zoom.get();
      if (p >= start && p <= end) return;
      const w = end - start;
      let s = p - w / 2;
      s = Math.max(0, Math.min(100 - w, s));
      zoom.set(s, s + w, true);
    },
    [zoom, layout],
  );

  /** Pie parts hidden from the legend are skipped (review A14). */
  const partHidden = useCallback(
    (i: number) => {
      const parts = layout.form === 'pie' || layout.form === 'donut';
      return parts && hidden.has(layout.model.x.cats[i]?.id ?? '');
    },
    [layout, hidden],
  );

  const go = useCallback(
    (i: number, dir: 1 | -1 = 1) => {
      const chart = instance.current;
      const n = layout.model.x.cats.length;
      if (n === 0) return;
      let next = Math.max(0, Math.min(n - 1, i));
      // Step over hidden pie parts; at an end, stay on the last shown one.
      while (partHidden(next) && next + dir >= 0 && next + dir < n) next += dir;
      if (partHidden(next)) {
        const back = index.current;
        if (back < 0 || partHidden(back)) {
          onLive({ name: '', text: 'All parts hidden. Use the legend to show one.' });
          return;
        }
        next = back;
      }
      index.current = next;
      keepInWindow(next);
      const seriesShown = layout.model.series.findIndex((s) => !hidden.has(s.id));
      if (seriesShown < 0 && !(layout.form === 'pie' || layout.form === 'donut' || layout.form === 'funnel')) {
        chart?.dispatchAction({ type: 'hideTip' });
        announce(next);
        return;
      }
      const seriesIndex = Math.max(0, seriesShown);
      const dataIndex = dataIndexOf(next);
      if (chart) {
        chart.dispatchAction({ type: 'downplay' });
        chart.dispatchAction({ type: 'highlight', seriesIndex, dataIndex });
        chart.dispatchAction({ type: 'showTip', seriesIndex, dataIndex });
      }
      announce(next);
    },
    [instance, layout, hidden, dataIndexOf, keepInWindow, announce, partHidden, onLive],
  );

  return useCallback(
    (e: React.KeyboardEvent<HTMLElement>) => {
      const form = layout.form;
      const n = layout.model.x.cats.length;
      const parts = form === 'pie' || form === 'donut' || form === 'funnel';
      const cur = index.current;
      const step = (d: 1 | -1) => go(cur < 0 ? (d > 0 ? 0 : n - 1) : cur + d, d);
      switch (e.key) {
        case 'ArrowRight':
          if (!CARTESIAN.has(form) && !parts) return;
          e.preventDefault();
          step(1);
          return;
        case 'ArrowLeft':
          if (!CARTESIAN.has(form) && !parts) return;
          e.preventDefault();
          step(-1);
          return;
        case 'ArrowDown':
          if (!parts) return;
          e.preventDefault();
          step(1);
          return;
        case 'ArrowUp':
          if (!parts) return;
          e.preventDefault();
          step(-1);
          return;
        case 'Home':
          e.preventDefault();
          go(0, 1);
          return;
        case 'End':
          e.preventDefault();
          go(n - 1, -1);
          return;
        case 'Escape': {
          const chart = instance.current;
          chart?.dispatchAction({ type: 'hideTip' });
          chart?.dispatchAction({ type: 'downplay' });
          index.current = -1;
          return;
        }
        case '+':
        case '=':
        case '-':
        case '−':
        case '0': {
          if (!zoom || !layout.zoom) return;
          e.preventDefault();
          if (e.key === '0') {
            zoom.reset();
            return;
          }
          zoom.zoomBy(e.key === '+' || e.key === '=' ? 1 / 1.5 : 1.5);
          return;
        }
        default:
      }
    },
    [layout, go, zoom, instance],
  );
}
