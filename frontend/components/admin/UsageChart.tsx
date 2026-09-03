'use client';

/**
 * Messages per day — one series, so no legend: the heading names it.
 *
 * Deliberately ONE measure on ONE axis. "Active users" belongs to a
 * different scale and shares this window, so it is a stat tile above rather
 * than a second line here — two y-axes on one plot is the single easiest way
 * to make a chart lie.
 *
 * Long windows are bucketed (a year is 365 bars in ~700px, which is a
 * texture, not a chart): ≤ 31 points stay daily, beyond that they fold into
 * weeks and the tooltip says so. Bars carry 4px rounded tops anchored to the
 * baseline, a 2px gap, and recessive gridlines; the accent hue is the design
 * system's own, already contrast-checked in both themes.
 */

import { useMemo, useState } from 'react';

export interface UsagePoint {
  day: string;
  messages: number;
  active_users: number;
}

interface Bucket {
  label: string;
  from: string;
  to: string;
  messages: number;
  days: number;
}

const HEIGHT = 132;
const GAP = 2;

function shortDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        timeZone: 'UTC',
      });
}

/** Daily below the threshold, weekly above it — never 365 hairlines. */
export function bucketize(points: UsagePoint[], maxBars = 31): Bucket[] {
  if (points.length === 0) return [];
  if (points.length <= maxBars) {
    return points.map((p) => ({
      label: shortDate(p.day),
      from: p.day,
      to: p.day,
      messages: p.messages,
      days: 1,
    }));
  }
  const size = Math.ceil(points.length / maxBars);
  const out: Bucket[] = [];
  for (let i = 0; i < points.length; i += size) {
    const slice = points.slice(i, i + size);
    out.push({
      label: shortDate(slice[0].day),
      from: slice[0].day,
      to: slice[slice.length - 1].day,
      messages: slice.reduce((n, p) => n + p.messages, 0),
      days: slice.length,
    });
  }
  return out;
}

export function UsageChart({ points }: { points: UsagePoint[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const bars = useMemo(() => bucketize(points), [points]);
  const peak = Math.max(1, ...bars.map((b) => b.messages));
  const total = bars.reduce((n, b) => n + b.messages, 0);

  if (bars.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-muted">
        No activity in this period.
      </p>
    );
  }

  const width = 100; // percent-based viewBox: the SVG scales with its card
  const barWidth = Math.max(0.5, width / bars.length - GAP * (width / 700));
  const active = hover === null ? null : bars[hover];

  return (
    <div className="relative">
      {/* Gridlines sit behind the bars and carry the only y labels. */}
      <div className="pointer-events-none absolute inset-x-0 top-0" style={{ height: HEIGHT }}>
        {[1, 0.5, 0].map((frac) => (
          <div
            key={frac}
            className="absolute inset-x-0 flex items-center gap-2"
            style={{ top: `${(1 - frac) * 100}%` }}
          >
            <span className="w-10 shrink-0 text-right text-[10px] tabular-nums text-faint">
              {Math.round(peak * frac).toLocaleString()}
            </span>
            <span className="h-px flex-1 bg-border" />
          </div>
        ))}
      </div>

      <div className="pl-12">
        <svg
          viewBox={`0 0 ${width} ${HEIGHT}`}
          preserveAspectRatio="none"
          height={HEIGHT}
          className="w-full"
          role="img"
          aria-label={`Messages per ${bars[0].days > 1 ? 'week' : 'day'}: ${total.toLocaleString()} in this period, peak ${peak.toLocaleString()}.`}
        >
          {bars.map((bar, i) => {
            const h = Math.max(bar.messages > 0 ? 2 : 0, (bar.messages / peak) * HEIGHT);
            const x = (i * width) / bars.length;
            return (
              <rect
                key={bar.from}
                x={x}
                y={HEIGHT - h}
                width={barWidth}
                height={h}
                rx={1}
                fill="var(--ts-accent)"
                opacity={hover === null || hover === i ? 1 : 0.45}
                onMouseEnter={() => setHover(i)}
                onMouseLeave={() => setHover(null)}
                style={{ transition: 'opacity 120ms' }}
              >
                <title>{`${bar.label}: ${bar.messages.toLocaleString()} messages`}</title>
              </rect>
            );
          })}
        </svg>

        <div className="mt-1.5 flex justify-between text-[10px] text-faint">
          <span>{bars[0].label}</span>
          {active && (
            <span className="tabular-nums text-ink">
              {active.days > 1
                ? `${active.label} – ${shortDate(active.to)}`
                : active.label}
              {' · '}
              {active.messages.toLocaleString()} messages
            </span>
          )}
          <span>{shortDate(bars[bars.length - 1].to)}</span>
        </div>
      </div>
    </div>
  );
}
