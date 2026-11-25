'use client';

/**
 * The HTML bar list (charts design v2 §4.2, C2): every horizontal-bar form —
 * `horizontal_bar`, bars whose labels do not fit as columns, pies with 7+
 * parts and funnels under 480 px.
 *
 * WHY HTML. ECharts gives every category band the same height, so one
 * 200-character label (sf_assistant keeps 200) made EVERY row about 190 px
 * tall on a phone, and a 120-row canvas at DPR 3 approaches iOS Safari's
 * canvas limit. Here each row is as tall as its own label, the text is real
 * (find-in-page, translation, text zoom, screen readers), and a long list
 * scrolls inside a capped viewport with Show all / Show fewer.
 *
 * Marks follow the dataviz spec: ≤ 20 px bars, 4 px rounded data end, square
 * at the baseline, one shared scale with a zero line, negatives growing left,
 * colours straight from `var(--ts-chart-n)` so the theme follows with no
 * JavaScript. Values print in text ink beside the bar, never inside a fill.
 *
 * ECharts is NOT imported here; it loads only if the reader asks for a PNG.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ChartModel } from '@/lib/chartModel';
import { axisTickFormatter, formatShare, formatValue } from '@/lib/chartFormat';
import { valueExtent } from '@/lib/chartLayout';
import { measurerFor } from '@/lib/chartText';

const useIsoLayoutEffect = typeof window !== 'undefined' ? useLayoutEffect : useEffect;

/** Rows above which jsdom/SSR (no measured heights) assumes the list is capped. */
const ESTIMATED_CAP_ROWS = 12;

export interface LiveMessage {
  name: string;
  text: string;
}

/** Scale-tick text size (CSS `.chart-tick`, 0.6875rem) relative to the 12 px measurer. */
const TICK_SCALE = 11 / 12;
const TICK_GAP = 8;

/**
 * Ticks for the shared scale, chosen for the measured track (review V3): the
 * most ticks (≤ 6) whose compact labels ("1.5M") fit side by side with an
 * 8 px gap, the end labels anchored inside the track. With no measured width
 * (SSR, jsdom) the old count of about 4 is kept.
 */
export function fitTicks(
  min: number,
  max: number,
  trackPx: number,
  measure: (t: string) => number,
): { ticks: number[]; label: (v: number) => string } {
  const label = axisTickFormatter(Math.max(Math.abs(min), Math.abs(max)));
  if (!(trackPx > 0)) return { ticks: niceTicks(min, max, 4), label };
  for (let count = 5; count >= 1; count--) {
    const ticks = niceTicks(min, max, count);
    const lo = Math.min(min, ticks[0]);
    const hi = Math.max(max, ticks[ticks.length - 1]);
    const span = hi - lo || 1;
    const boxes = ticks.map((t, k) => {
      const x = ((t - lo) / span) * trackPx;
      const w = measure(label(t)) * TICK_SCALE;
      if (k === 0) return [x, x + w];
      if (k === ticks.length - 1) return [x - w, x];
      return [x - w / 2, x + w / 2];
    });
    if (boxes.every((b, k) => k === 0 || b[0] - boxes[k - 1][1] >= TICK_GAP)) return { ticks, label };
  }
  // Even two labels collide (a very narrow track): the ends only.
  const ticks = niceTicks(min, max, 1);
  return { ticks: [ticks[0], ticks[ticks.length - 1]], label };
}

function niceTicks(min: number, max: number, count = 4): number[] {
  const span = max - min || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const out: number[] = [];
  for (let v = lo; v <= hi + step / 2; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

function color(slot: number): string {
  return `var(--ts-chart-${(slot % 8) + 1})`;
}

export function ChartBarList({
  model,
  ordered,
  percentOfFirst,
  shareOfTotal,
  cap,
  hidden,
  reducedMotion,
  labelledBy,
  describedBy,
  onFocusVisible,
  onLive,
}: {
  model: ChartModel;
  ordered: boolean;
  /** Funnel stages: show each value's share of the first stage. */
  percentOfFirst: boolean;
  /** A pie drawn as bars: show each value's share of the total. */
  shareOfTotal: boolean;
  cap: string;
  hidden: ReadonlySet<string>;
  reducedMotion: boolean;
  labelledBy: string;
  /** Summary + key hint ids (review A10). */
  describedBy?: string;
  /** Keyboard focus on the list, so the figure can show the key hint. */
  onFocusVisible?: (visible: boolean) => void;
  onLive?: (msg: LiveMessage) => void;
}) {
  const cats = model.x.cats;
  const n = cats.length;
  const visible = useMemo(() => model.series.filter((s) => !hidden.has(s.id)), [model.series, hidden]);
  const multi = model.series.length > 1;
  const stacked = model.stacked && multi;

  // The tick row's measured width, for the tick count (review V3).
  const tickRowRef = useRef<HTMLSpanElement | null>(null);
  const [trackPx, setTrackPx] = useState(0);
  useIsoLayoutEffect(() => {
    const el = tickRowRef.current;
    if (!el) return;
    const read = () => {
      const w = Math.floor(el.getBoundingClientRect().width);
      setTrackPx((prev) => (prev === w ? prev : w));
    };
    read();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, [multi]);

  // One shared scale across rows (visible series only); multi-series lists
  // round it to nice ticks so the gridlines mean something.
  const scale = useMemo(() => {
    const ext = valueExtent({ ...model, series: visible.length ? visible : model.series });
    let min = Math.min(0, ext.min);
    let max = Math.max(0, ext.max);
    if (min === max) max = 1;
    const fit = multi ? fitTicks(min, max, trackPx, measurerFor()) : null;
    const ticks = fit?.ticks ?? [];
    if (ticks.length) {
      min = Math.min(min, ticks[0]);
      max = Math.max(max, ticks[ticks.length - 1]);
    }
    return { min, max, ticks, tickLabel: fit?.label ?? formatValue };
  }, [model, visible, multi, trackPx]);
  const span = scale.max - scale.min || 1;
  const pct = (v: number) => ((v - scale.min) / span) * 100;
  const zero = pct(0);

  const [active, setActive] = useState<number | null>(null);
  /** The active row came from the pointer (Esc anywhere dismisses it, review A12). */
  const [byPointer, setByPointer] = useState(false);
  const keyMoved = useRef(false);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const rowRefs = useRef<Array<HTMLLIElement | null>>([]);

  useIsoLayoutEffect(() => {
    const el = viewportRef.current;
    if (!el) return;
    const check = () => {
      if (el.scrollHeight === 0 && el.clientHeight === 0) setOverflowing(n > ESTIMATED_CAP_ROWS);
      else setOverflowing(el.scrollHeight > el.clientHeight + 1);
    };
    check();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, [n, expanded, hidden]);

  const total = model.valueFlags.total;
  const first = model.series[0]?.values[0] ?? null;
  const percentWhole = Boolean(model.valueFlags.percentWhole);

  function valueText(i: number): string[] {
    if (stacked) {
      const sum = visible.reduce((a, s) => a + (s.values[i] ?? 0), 0);
      return [formatValue(sum)];
    }
    if (multi) return visible.map((s) => formatValue(s.values[i] ?? null));
    const v = model.series[0]?.values[i] ?? null;
    let text = percentWhole && v !== null ? `${formatValue(v)}%` : formatValue(v);
    if (shareOfTotal && !percentWhole && v !== null) text += ` · ${formatShare(v, total)}`;
    if (percentOfFirst && v !== null && first) text += ` (${formatShare(v, first)})`;
    return [text];
  }

  function announce(i: number) {
    if (!onLive || i < 0 || i >= n) return;
    const parts = visible.map((s) => `${multi ? `${s.label} ` : ''}${formatValue(s.values[i] ?? null)}`);
    onLive({ name: cats[i].label, text: parts.join(', ') });
  }

  function move(next: number) {
    const i = Math.max(0, Math.min(n - 1, next));
    keyMoved.current = true;
    setByPointer(false);
    setActive(i);
    announce(i);
  }

  // Scroll after the row (and its popover) rendered, so both are in view (review A5).
  useEffect(() => {
    if (active === null || !keyMoved.current) return;
    keyMoved.current = false;
    rowRefs.current[active]?.scrollIntoView?.({ block: 'nearest' });
  }, [active]);

  useEffect(() => {
    if (!byPointer || active === null || typeof document === 'undefined') return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setActive(null);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [byPointer, active]);

  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      move(active === null ? 0 : active + 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      move(active === null ? 0 : active - 1);
    } else if (e.key === 'Home') {
      e.preventDefault();
      move(0);
    } else if (e.key === 'End') {
      e.preventDefault();
      move(n - 1);
    } else if (e.key === 'Escape') {
      setActive(null);
    }
  }

  const grow = reducedMotion ? '' : ' chart-bar-grow';

  function swatch(slot: number) {
    return <span aria-hidden className="chart-value-swatch" style={{ background: color(slot) }} />;
  }

  /** The values column: series names for screen readers, a colour key for the eye (review A4). */
  function valueCells(i: number): React.ReactNode {
    if (stacked) {
      const parts = visible
        .filter((s) => s.values[i] !== null && s.values[i] !== undefined)
        .map((s) => `${s.label} ${formatValue(s.values[i] ?? null)}`);
      return (
        <span className="block">
          {parts.length > 0 && <span className="sr-only">{parts.join(', ')}, total </span>}
          {valueText(i)[0]}
        </span>
      );
    }
    if (multi) {
      return visible.map((s) => (
        <span key={s.id} className="chart-value-line">
          {swatch(s.slot)}
          <span className="sr-only">{s.label} </span>
          {formatValue(s.values[i] ?? null)}
        </span>
      ));
    }
    return <span className="block">{valueText(i)[0]}</span>;
  }

  function bar(key: string, v: number | null, slot: number, extraClass = '') {
    if (v === null || v === 0) return null;
    const neg = v < 0;
    const left = neg ? pct(v) : zero;
    const width = Math.abs(pct(v) - zero);
    return (
      <span
        key={key}
        aria-hidden
        data-neg={neg ? 'true' : 'false'}
        className={`chart-bar${grow}${extraClass}`}
        style={{
          left: `${left}%`,
          width: `${width}%`,
          background: color(slot),
          borderRadius: neg ? '4px 0 0 4px' : '0 4px 4px 0',
        }}
      />
    );
  }

  function stackBars(i: number) {
    let pos = 0;
    let neg = 0;
    const segs: React.ReactNode[] = [];
    const last = new Map<boolean, string>();
    for (const s of visible) {
      const v = s.values[i];
      if (v === null || v === undefined || v === 0) continue;
      last.set(v < 0, s.id);
    }
    for (const s of visible) {
      const v = s.values[i];
      if (v === null || v === undefined || v === 0) continue;
      const isNeg = v < 0;
      const from = isNeg ? neg + v : pos;
      const to = isNeg ? neg : pos + v;
      if (isNeg) neg += v;
      else pos += v;
      const outer = last.get(isNeg) === s.id;
      const l = pct(from);
      const w = pct(to) - l;
      segs.push(
        <span
          key={s.id}
          aria-hidden
          data-neg={isNeg ? 'true' : 'false'}
          className={`chart-bar${grow}`}
          style={{
            left: `${l}%`,
            // 2 px surface gap between touching segments: a real gap, so it
            // works on every background the list sits on.
            width: `calc(${w}% - ${outer ? 0 : 2}px)`,
            marginLeft: isNeg && !outer ? '2px' : undefined,
            background: color(s.slot),
            borderRadius: outer ? (isNeg ? '4px 0 0 4px' : '0 4px 4px 0') : 0,
          }}
        />,
      );
    }
    return segs;
  }

  const List = ordered ? 'ol' : 'ul';
  const capped = !expanded;

  return (
    <div className="min-w-0">
      <div
        ref={viewportRef}
        className="chart-bars-viewport"
        style={capped ? { maxHeight: cap } : undefined}
        tabIndex={0}
        // A group, not a region: one landmark per chart would crowd the
        // landmark list of a long conversation (review A10).
        role="group"
        aria-roledescription="bar chart"
        aria-labelledby={labelledBy}
        aria-describedby={describedBy}
        onKeyDown={onKeyDown}
        onFocus={(e) => {
          let visible = true;
          try {
            visible = e.currentTarget.matches(':focus-visible');
          } catch {
            visible = true;
          }
          onFocusVisible?.(visible);
        }}
        onBlur={() => {
          setActive(null);
          onFocusVisible?.(false);
        }}
      >
        <List className="chart-bars" data-active={active !== null ? 'true' : 'false'}>
          {multi && (
            <li aria-hidden className="chart-bars-ticks">
              <span className="chart-bar-label" />
              <span className="chart-tick-row" ref={tickRowRef}>
                {scale.ticks.map((t, k) => (
                  <span
                    key={t}
                    className="chart-tick"
                    data-edge={k === 0 ? 'start' : k === scale.ticks.length - 1 ? 'end' : undefined}
                    style={{ left: `${pct(t)}%` }}
                  >
                    {scale.tickLabel(t)}
                  </span>
                ))}
              </span>
              <span />
            </li>
          )}
          {cats.map((cat, i) => {
            return (
              <li
                key={cat.id}
                ref={(el) => {
                  rowRefs.current[i] = el;
                }}
                data-active={active === i ? 'true' : undefined}
                onMouseEnter={() => {
                  setByPointer(true);
                  setActive(i);
                }}
                onMouseLeave={() => setActive(null)}
              >
                <span className="chart-bar-label" dir="auto">
                  {cat.label}
                </span>
                <span className="chart-bar-track">
                  {multi &&
                    scale.ticks.map((t) => (
                      <span key={t} aria-hidden className="chart-bar-grid" style={{ left: `${pct(t)}%` }} />
                    ))}
                  {(scale.min < 0 || multi) && (
                    <span aria-hidden className="chart-bar-zero" style={{ left: `${zero}%` }} />
                  )}
                  {stacked ? (
                    stackBars(i)
                  ) : multi ? (
                    <span className="chart-bar-group">
                      {visible.map((s) => (
                        <span key={s.id} className="chart-bar-lane">
                          {bar(s.id, s.values[i] ?? null, s.slot)}
                        </span>
                      ))}
                    </span>
                  ) : (
                    bar('v', model.series[0]?.values[i] ?? null, model.series[0]?.slot ?? 0)
                  )}
                </span>
                <span className="chart-bar-values">{valueCells(i)}</span>
                {multi && active === i && (
                  // An overlay anchored to the row (review A5/V7): rows never
                  // move under the pointer. The last rows open it upwards.
                  <span
                    className="chart-bar-popover"
                    role="note"
                    data-flip={i >= n - 2 && n > 2 ? 'up' : undefined}
                  >
                    {visible.map((s) => (
                      <span key={s.id} className="flex items-baseline gap-1.5">
                        <span
                          aria-hidden
                          className="chart-popover-dot inline-block h-2 w-2 shrink-0 rounded-full"
                          style={{ background: color(s.slot) }}
                        />
                        <b className="tabular-nums">{formatValue(s.values[i] ?? null)}</b>
                        <span dir="auto" className="break-words [overflow-wrap:anywhere]">
                          {s.label}
                        </span>
                      </span>
                    ))}
                  </span>
                )}
              </li>
            );
          })}
        </List>
      </div>
      {(overflowing || expanded) && (
        <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
          <span>
            {n.toLocaleString()} rows{expanded ? '' : ' · scroll for more'}
          </span>
          <button
            type="button"
            className="chart-btn"
            aria-expanded={expanded}
            onClick={() => {
              setExpanded((e) => !e);
              if (expanded) viewportRef.current?.scrollTo?.({ top: 0 });
            }}
          >
            {expanded ? 'Show fewer' : `Show all ${n.toLocaleString()}`}
          </button>
        </div>
      )}
    </div>
  );
}
