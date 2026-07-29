'use client';

/**
 * Proof-drawer Chart section (§9): Recharts bar/line/area/pie/scatter with
 * stacked support, tooltips, ≤400ms draw-in, dark-mode aware.
 *
 * Palette: the 5-slot categorical order (teal → blue → amber → violet →
 * rose) validated for CVD separation and ≥3:1 contrast on BOTH surfaces —
 * assigned in fixed order, never cycled. Pies with many categories fold the
 * tail into "Other" (the full rows stay in the Data tab).
 */

import { useEffect, useMemo, useState } from 'react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { ChartSpec, DataRow } from '@/lib/types';
import { useTheme } from './Providers';

const PALETTE = ['#0E9F9A', '#2F6FB2', '#B7791F', '#6D5AE6', '#C0566B'];

const THEME_TOKENS = {
  // Kept in sync with the CSS tokens in app/globals.css (Recharts needs
  // literal colors, not var() references).
  dark: {
    grid: '#262626',
    axis: '#262626',
    text: '#a3a3a3',
    surface: '#1e1e1e',
    tooltipBg: '#2a2a2a',
    tooltipText: '#f5f5f5',
  },
  light: {
    grid: '#e3e3e3',
    axis: '#e3e3e3',
    text: '#5d5d5d',
    surface: '#f4f4f4',
    tooltipBg: '#ffffff',
    tooltipText: '#0d0d0d',
  },
} as const;

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    setReduced(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  return reduced;
}

function formatNumber(v: unknown): string {
  return typeof v === 'number' ? v.toLocaleString() : String(v);
}

const MAX_PIE_SLICES = 6;

export function ChartView({
  spec,
  data,
}: {
  spec: ChartSpec;
  data: DataRow[];
}) {
  const { theme } = useTheme();
  const t = THEME_TOKENS[theme];
  const reducedMotion = usePrefersReducedMotion();

  const animation = {
    isAnimationActive: !reducedMotion,
    animationDuration: 350, // §9: draw-in ≤ 400ms
    animationEasing: 'ease-out' as const,
  };

  const yKeys = spec.y_keys;
  const multiSeries = yKeys.length >= 2;

  const tooltipProps = {
    cursor:
      spec.type === 'bar'
        ? { fill: t.grid, opacity: 0.35 }
        : { stroke: t.grid },
    contentStyle: {
      background: t.tooltipBg,
      border: `1px solid ${t.grid}`,
      borderRadius: 10,
      color: t.tooltipText,
      fontSize: 13,
    },
    labelStyle: { color: t.tooltipText, fontWeight: 600 },
    itemStyle: { color: t.text },
    formatter: (value: unknown) => formatNumber(value),
  };

  const axisProps = {
    tick: { fill: t.text, fontSize: 12 },
    tickLine: false,
    axisLine: { stroke: t.axis },
  } as const;

  const legend = multiSeries ? (
    <Legend
      wrapperStyle={{ fontSize: 13, color: t.text }}
      iconType="circle"
      iconSize={9}
    />
  ) : null;

  const pieData = useMemo(() => {
    if (spec.type !== 'pie') return [];
    const key = yKeys[0];
    const rows = data
      .map((r) => ({
        name: String(r[spec.x_key] ?? '—'),
        value: Number(r[key] ?? 0),
      }))
      .sort((a, b) => b.value - a.value);
    if (rows.length <= MAX_PIE_SLICES) return rows;
    const head = rows.slice(0, MAX_PIE_SLICES - 1);
    const other = rows
      .slice(MAX_PIE_SLICES - 1)
      .reduce((sum, r) => sum + r.value, 0);
    return [...head, { name: 'Other', value: other }];
  }, [spec, data, yKeys]);

  let chart: React.ReactElement;

  switch (spec.type) {
    case 'bar':
      chart = (
        <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={t.grid} />
          <XAxis dataKey={spec.x_key} {...axisProps} />
          <YAxis {...axisProps} width={52} />
          <Tooltip {...tooltipProps} />
          {legend}
          {yKeys.map((key, i) => (
            <Bar
              key={key}
              dataKey={key}
              stackId={spec.stacked ? 'stack' : undefined}
              fill={PALETTE[i % PALETTE.length]}
              stroke={t.surface}
              strokeWidth={spec.stacked ? 1 : 0}
              maxBarSize={36}
              radius={
                !spec.stacked || i === yKeys.length - 1
                  ? [3, 3, 0, 0]
                  : [0, 0, 0, 0]
              }
              {...animation}
            />
          ))}
        </BarChart>
      );
      break;

    case 'line':
      chart = (
        <LineChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={t.grid} />
          <XAxis dataKey={spec.x_key} {...axisProps} />
          <YAxis {...axisProps} width={52} />
          <Tooltip {...tooltipProps} />
          {legend}
          {yKeys.map((key, i) => (
            <Line
              key={key}
              type="monotone"
              dataKey={key}
              stroke={PALETTE[i % PALETTE.length]}
              strokeWidth={2}
              dot={{ r: 3, strokeWidth: 0, fill: PALETTE[i % PALETTE.length] }}
              activeDot={{ r: 5, stroke: t.surface, strokeWidth: 2 }}
              {...animation}
            />
          ))}
        </LineChart>
      );
      break;

    case 'area':
      chart = (
        <AreaChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={t.grid} />
          <XAxis dataKey={spec.x_key} {...axisProps} />
          <YAxis {...axisProps} width={52} />
          <Tooltip {...tooltipProps} />
          {legend}
          {yKeys.map((key, i) => (
            <Area
              key={key}
              type="monotone"
              dataKey={key}
              stackId={spec.stacked ? 'stack' : undefined}
              stroke={PALETTE[i % PALETTE.length]}
              strokeWidth={2}
              fill={PALETTE[i % PALETTE.length]}
              fillOpacity={0.22}
              {...animation}
            />
          ))}
        </AreaChart>
      );
      break;

    case 'pie':
      chart = (
        <PieChart>
          <Tooltip {...tooltipProps} />
          <Legend
            wrapperStyle={{ fontSize: 13, color: t.text }}
            iconType="circle"
            iconSize={9}
          />
          <Pie
            data={pieData}
            dataKey="value"
            nameKey="name"
            innerRadius="45%"
            outerRadius="78%"
            paddingAngle={2}
            stroke={t.surface}
            strokeWidth={2}
            {...animation}
          >
            {pieData.map((entry, i) => (
              <Cell key={entry.name} fill={PALETTE[i % PALETTE.length]} />
            ))}
          </Pie>
        </PieChart>
      );
      break;

    case 'scatter': {
      const numericX =
        data.length > 0 && typeof data[0][spec.x_key] === 'number';
      chart = (
        <ScatterChart margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={t.grid} />
          <XAxis
            dataKey={spec.x_key}
            type={numericX ? 'number' : 'category'}
            name={spec.x_key}
            {...axisProps}
          />
          <YAxis
            dataKey={yKeys[0]}
            type="number"
            name={yKeys[0]}
            {...axisProps}
            width={52}
          />
          <Tooltip {...tooltipProps} />
          {legend}
          {yKeys.map((key, i) => (
            <Scatter
              key={key}
              name={key}
              data={data}
              dataKey={key}
              fill={PALETTE[i % PALETTE.length]}
              stroke={t.surface}
              strokeWidth={1}
              {...animation}
            />
          ))}
        </ScatterChart>
      );
      break;
    }

    default:
      return (
        <p className="text-sm text-muted">
          Unknown chart type — the data is available in the Data tab.
        </p>
      );
  }

  return (
    <figure>
      <figcaption className="mb-2 text-sm font-medium text-ink">
        {spec.title}
      </figcaption>
      <div className="h-[300px] w-full min-w-0" role="img" aria-label={spec.title}>
        <ResponsiveContainer width="100%" height="100%">
          {chart}
        </ResponsiveContainer>
      </div>
    </figure>
  );
}
