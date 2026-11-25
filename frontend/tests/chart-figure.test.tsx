// @vitest-environment jsdom
/**
 * ChartFigure — the shell around every chat chart (charts design v2 §8).
 *
 * jsdom has no canvas, so `components/EChart` is replaced by a stub that
 * records every option it is handed and exposes a fake instance
 * (`dispatchAction`, `resize`, `getOption`). What is under test is the HTML
 * the reader uses — title, summary, legend, toolbar, keyboard, live region,
 * table — and which options reach ECharts, not ECharts' drawing. Made-up data.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  options: [] as Array<Record<string, unknown>>,
  heights: [] as number[],
  actions: [] as Array<Record<string, unknown>>,
  zoom: { start: 60, end: 100 },
  events: {} as Record<string, (p?: unknown) => void>,
}));

vi.mock('@/components/EChart', () => ({
  default: (props: {
    option: Record<string, unknown>;
    height: number;
    onReady?: (i: unknown) => void;
    onEvents?: Record<string, (p?: unknown) => void>;
  }) => {
    if (h.options[h.options.length - 1] !== props.option) h.options.push(props.option);
    h.heights.push(props.height);
    h.events = props.onEvents ?? {};
    props.onReady?.({
      // Like ECharts: a dataZoom action moves the window and emits
      // `datazoom` synchronously.
      dispatchAction: (a: Record<string, unknown>) => {
        h.actions.push(a);
        if (a.type === 'dataZoom') {
          if (typeof a.start === 'number' && typeof a.end === 'number') h.zoom = { start: a.start, end: a.end };
          h.events.datazoom?.();
        }
      },
      resize: () => undefined,
      getOption: () => ({ dataZoom: [h.zoom] }),
    });
    return <div data-testid="echart-stub" style={{ height: props.height }} />;
  },
}));

// A real context, as in the app: a theme change reaches the memoised figure.
vi.mock('@/components/Providers', async () => {
  const React = await import('react');
  const Ctx = React.createContext({ theme: 'dark', toggleTheme: () => undefined });
  return {
    useTheme: () => React.useContext(Ctx),
    TestThemeProvider: Ctx.Provider,
  };
});

vi.mock('@/lib/chartExport', () => ({
  exportChartPng: vi.fn(async () => ({ ok: false, reason: 'too-tall' })),
}));

import { ChartView } from '@/components/ChartView';
import * as ProvidersModule from '@/components/Providers';
import { exportChartPng } from '@/lib/chartExport';
import type { ChartSpec, DataRow } from '@/lib/types';

const SPEC: ChartSpec = {
  type: 'bar',
  x_key: 'region',
  y_keys: ['open_cases', 'closed_cases'],
  title: 'Cases by region',
  stacked: false,
};
const ROWS: DataRow[] = [
  { region: 'North', open_cases: 12, closed_cases: 30 },
  { region: 'South', open_cases: 8, closed_cases: 22 },
  { region: 'East', open_cases: 15, closed_cases: 18 },
];

let rectWidth = 720;
let roCallbacks: Array<() => void> = [];

beforeEach(() => {
  h.options.length = 0;
  h.heights.length = 0;
  h.actions.length = 0;
  h.zoom = { start: 60, end: 100 };
  rectWidth = 720;
  roCallbacks = [];
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(
    () => ({ width: rectWidth, height: 400, top: 0, left: 0, right: rectWidth, bottom: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect,
  );
  vi.stubGlobal(
    'ResizeObserver',
    class {
      cb: () => void;
      constructor(cb: () => void) {
        this.cb = cb;
        roCallbacks.push(cb);
      }
      observe() {}
      disconnect() {}
      unobserve() {}
    },
  );
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    cb(0);
    return 1;
  });
  vi.stubGlobal('cancelAnimationFrame', () => undefined);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function renderChart(spec: ChartSpec = SPEC, rows: DataRow[] = ROWS) {
  const utils = render(<ChartView spec={spec} data={rows} context="inline" />);
  await screen.findByTestId('echart-stub');
  return utils;
}

describe('figure chrome', () => {
  it('names the plot by the title and describes it by the summary and hint', async () => {
    await renderChart();
    const plot = screen.getByTestId('chart-plot');
    expect(plot.getAttribute('role')).toBe('group');
    expect(plot.getAttribute('aria-roledescription')).toBe('chart');
    const title = document.getElementById(plot.getAttribute('aria-labelledby')!);
    expect(title?.textContent).toBe('Cases by region');
    const [summaryId, hintId] = plot.getAttribute('aria-describedby')!.split(' ');
    expect(document.getElementById(summaryId)?.textContent).toMatch(/Bar chart of 2 series by Region, 3 categories/);
    expect(document.getElementById(hintId)?.textContent).toMatch(/Arrow keys read the values/);
  });

  it('reserves the planned height before the chunk loads', async () => {
    await renderChart();
    const reserved = screen.getByTestId('chart-reserved');
    expect(parseInt(reserved.style.height, 10)).toBeGreaterThan(280);
    expect(h.heights[0]).toBe(parseInt(reserved.style.height, 10));
  });
});

describe('HTML legend', () => {
  it('shows full names and toggles by id', async () => {
    await renderChart();
    const legend = screen.getByRole('group', { name: 'Legend' });
    const open = within(legend).getByRole('button', { name: /Open cases/ });
    expect(open.getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(open);
    expect(open.getAttribute('aria-pressed')).toBe('false');
    expect(h.actions).toContainEqual({ type: 'legendToggleSelect', name: 's0' });
  });

  it('keeps legend and zoom state through a width change and a theme change', async () => {
    const rows: DataRow[] = Array.from({ length: 95 }, (_, i) => ({
      day: new Date(Date.UTC(2025, 0, 1) + i * 5 * 86_400_000).toISOString().slice(0, 10),
      a: i % 7,
      b: (i * 3) % 5,
    }));
    const spec: ChartSpec = { type: 'bar', x_key: 'day', y_keys: ['a', 'b'], title: 'Daily', stacked: false };
    const Theme = (ProvidersModule as unknown as {
      TestThemeProvider: React.Provider<{ theme: string; toggleTheme: () => void }>;
    }).TestThemeProvider;
    const view = (theme: string) => (
      <Theme value={{ theme, toggleTheme: () => undefined }}>
        <ChartView spec={spec} data={rows} context="inline" />
      </Theme>
    );
    const { rerender } = render(view('dark'));
    await screen.findByTestId('echart-stub');
    fireEvent.click(screen.getByRole('button', { name: /^B/ }));
    const before = h.options.length;
    // A pure width change (+8 px) is a resize, not a new option.
    rectWidth = 728;
    act(() => roCallbacks.forEach((cb) => cb()));
    expect(h.options.length).toBe(before);
    // A theme change rebuilds the option, and the hidden series stays hidden.
    rerender(view('light'));
    await waitFor(() => expect(h.options.length).toBeGreaterThan(before));
    const last = h.options[h.options.length - 1];
    expect((last.legend as { selected: Record<string, boolean> }).selected.s1).toBe(false);
    // The first draw animates in; the rebuild does not replay it.
    expect(h.options[0].animationDuration).toBe(300);
    expect(last.animationDuration).toBe(0);
  });
});

describe('toolbar', () => {
  it('opens the table with aria-expanded and aria-controls', async () => {
    await renderChart();
    const btn = screen.getByRole('button', { name: /Table/ });
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    const region = document.getElementById(btn.getAttribute('aria-controls')!);
    expect(region?.hidden).toBe(true);
    fireEvent.click(btn);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(region?.hidden).toBe(false);
    expect(within(region!).getAllByRole('columnheader').map((c) => c.textContent)).toEqual([
      expect.stringContaining('region'),
      expect.stringContaining('open_cases'),
      expect.stringContaining('closed_cases'),
    ]);
  });

  it('loads the export lazily and reports a chart too tall to save', async () => {
    await renderChart();
    fireEvent.click(screen.getByRole('button', { name: 'Download chart as PNG' }));
    await screen.findByText(/Too tall to save as an image/);
    expect(exportChartPng).toHaveBeenCalledTimes(1);
  });
});

describe('keyboard', () => {
  it('arrow keys show the tip, highlight and announce; Tab, Space and PageDown pass through', async () => {
    await renderChart();
    const plot = screen.getByTestId('chart-plot');
    plot.focus();
    fireEvent.keyDown(plot, { key: 'ArrowRight' });
    expect(h.actions).toContainEqual({ type: 'showTip', seriesIndex: 0, dataIndex: 0 });
    expect(h.actions).toContainEqual({ type: 'highlight', seriesIndex: 0, dataIndex: 0 });
    expect(screen.getByTestId('chart-live').textContent).toBe('North: Open cases 12, Closed cases 30');
    fireEvent.keyDown(plot, { key: 'ArrowRight' });
    expect(screen.getByTestId('chart-live').textContent).toMatch(/^South:/);
    for (const key of ['Tab', ' ', 'PageDown']) {
      expect(fireEvent.keyDown(plot, { key })).toBe(true); // not prevented
    }
  });
});

describe('motion and states', () => {
  it('turns animation off under prefers-reduced-motion', async () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q.includes('reduce'),
      media: q,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    await renderChart();
    await waitFor(() => expect(h.options[h.options.length - 1].animation).toBe(false));
  });

  it('shows an empty state with the table open for an all-zero donut', async () => {
    render(
      <ChartView
        spec={{ type: 'donut', x_key: 'k', y_keys: ['v'], title: 'Zeros', stacked: false }}
        data={[
          { k: 'a', v: 0 },
          { k: 'b', v: 0 },
        ]}
      />,
    );
    expect(screen.getByText('Every value is 0, so there are no shares to draw.')).toBeTruthy();
    expect(screen.getByRole('button', { name: /Table/ }).getAttribute('aria-expanded')).toBe('true');
    expect(screen.queryByTestId('echart-stub')).toBeNull();
  });

  it('shows a pie legend with values and shares, and the donut total', async () => {
    await renderChart(
      { type: 'donut', x_key: 'k', y_keys: ['v'], title: 'Split', stacked: false },
      [
        { k: 'Alpha', v: 3 },
        { k: 'Beta', v: 1 },
      ],
    );
    const parts = screen.getByRole('group', { name: 'Parts' });
    expect(parts.textContent).toContain('Alpha');
    expect(parts.textContent).toContain('75.0%');
    expect(screen.getByText('Total')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Review fixes (2026-11-25): focus, zoom buttons, live region, keyboard over
// hidden parts, first-render motion, malformed specs. Made-up data.
// ---------------------------------------------------------------------------

const DAILY_SPEC: ChartSpec = { type: 'bar', x_key: 'day', y_keys: ['n'], title: 'Daily', stacked: false };
const DAILY: DataRow[] = Array.from({ length: 400 }, (_, i) => ({
  day: new Date(Date.UTC(2025, 0, 1) + i * 86_400_000).toISOString().slice(0, 10),
  n: (i * 7) % 11,
}));

describe('review fixes: zoom tools and focus', () => {
  it('A7: a windowed chart has single-tap Zoom in / Zoom out next to Reset; A1: Reset keeps focus', async () => {
    await renderChart(DAILY_SPEC, DAILY);
    const zoomIn = screen.getByRole('button', { name: 'Zoom in' });
    const zoomOut = screen.getByRole('button', { name: 'Zoom out' });
    const reset = screen.getByRole('button', { name: /Reset zoom/ });
    expect(reset.getAttribute('aria-disabled')).toBe('true');
    h.actions.length = 0;
    fireEvent.click(zoomIn);
    const step = h.actions.find((a) => a.type === 'dataZoom') as { start: number; end: number };
    expect(step.end - step.start).toBeLessThan(40);
    // The chart reports the new window; Reset becomes available.
    h.zoom = { start: step.start, end: step.end };
    act(() => h.events.datazoom?.());
    expect(reset.getAttribute('aria-disabled')).toBe('false');
    reset.focus();
    fireEvent.click(reset);
    expect(document.activeElement).toBe(reset);
    expect(reset.isConnected).toBe(true);
    expect(zoomOut.isConnected).toBe(true);
    h.zoom = { start: 60, end: 100 };
  });

  it('R2/A7: Zoom out stops at the span the category labels were planned for', async () => {
    const rows = Array.from({ length: 40 }, (_, i) => ({ office: `Regional office ${i + 1} east`, n: i }));
    await renderChart({ type: 'line', x_key: 'office', y_keys: ['n'], title: 'Offices', stacked: false }, rows);
    const dz = (h.options[h.options.length - 1].dataZoom as Array<{ maxValueSpan?: number; endValue: number }>)[0];
    expect(dz.maxValueSpan).toBe(dz.endValue);
    const maxPct = (dz.maxValueSpan! / 39) * 100;
    h.zoom = { start: 0, end: maxPct };
    act(() => h.events.datazoom?.());
    const out = screen.getByRole('button', { name: 'Zoom out' });
    expect(out.getAttribute('aria-disabled')).toBe('true');
    h.actions.length = 0;
    fireEvent.click(out);
    expect(h.actions.find((a) => a.type === 'dataZoom')).toBeUndefined();
    // The − key is held to the same limit.
    const plot = screen.getByTestId('chart-plot');
    fireEvent.keyDown(plot, { key: '-' });
    const keyed = h.actions.find((a) => a.type === 'dataZoom') as { start: number; end: number } | undefined;
    if (keyed) expect(keyed.end - keyed.start).toBeLessThanOrEqual(maxPct + 1e-6);
    h.zoom = { start: 60, end: 100 };
  });

  it('A2: Show all hands focus to the first legend item instead of the page', async () => {
    await renderChart();
    const legend = screen.getByRole('group', { name: 'Legend' });
    for (const b of within(legend).getAllByRole('button')) fireEvent.click(b);
    const showAll = within(legend).getByRole('button', { name: 'Show all' });
    showAll.focus();
    fireEvent.click(showAll);
    expect(document.activeElement).toBe(within(legend).getAllByRole('button')[0]);
  });

  it('A3: Download keeps focus (aria-disabled, never disabled) and says what happened', async () => {
    await renderChart();
    const btn = screen.getByRole('button', { name: 'Download chart as PNG' });
    btn.focus();
    fireEvent.click(btn);
    expect(btn.hasAttribute('disabled')).toBe(false);
    expect(document.activeElement).toBe(btn);
    await screen.findByText(/Too tall to save as an image/);
    expect(screen.getByTestId('chart-live').textContent).toBe('The image was not saved.');
  });

  it('A11: a chart drawn as a table has no Table toggle that cannot close it', () => {
    const rows = Array.from({ length: 250 }, (_, i) => ({ region: `R${i}`, open_cases: i, closed_cases: 1 }));
    render(<ChartView spec={SPEC} data={rows} />);
    expect(screen.getByText(/Too many categories/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /^Table$/ })).toBeNull();
    expect(screen.getAllByRole('columnheader').length).toBeGreaterThan(0);
  });
});

describe('review fixes: keyboard, live region, motion', () => {
  it('A12: Esc anywhere hides a tooltip the pointer opened', async () => {
    await renderChart();
    const plot = screen.getByTestId('chart-plot');
    fireEvent.pointerEnter(plot);
    h.actions.length = 0;
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(h.actions).toContainEqual({ type: 'hideTip' });
    fireEvent.pointerLeave(plot);
    h.actions.length = 0;
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(h.actions).not.toContainEqual({ type: 'hideTip' });
  });

  it('A13: pressing End at the end announces again (the live node is replaced)', async () => {
    await renderChart();
    const plot = screen.getByTestId('chart-plot');
    fireEvent.keyDown(plot, { key: 'End' });
    const live = screen.getByTestId('chart-live');
    const first = live.firstElementChild;
    expect(live.textContent).toMatch(/^East:/);
    fireEvent.keyDown(plot, { key: 'End' });
    expect(live.textContent).toMatch(/^East:/);
    expect(live.firstElementChild).not.toBe(first);
  });

  it('A13: a window that only follows the point being read does not overwrite its announcement', async () => {
    vi.useFakeTimers();
    try {
      await act(async () => {
        render(<ChartView spec={DAILY_SPEC} data={DAILY} context="inline" />);
      });
      const plot = screen.getByTestId('chart-plot');
      fireEvent.keyDown(plot, { key: 'Home' }); // outside the opening window → a quiet shift
      expect(h.actions.some((a) => a.type === 'dataZoom')).toBe(true);
      act(() => {
        vi.advanceTimersByTime(600);
      });
      expect(screen.getByTestId('chart-live').textContent).toMatch(/^1 Jan 2025:/);
      // A zoom the reader asks for is still announced.
      fireEvent.keyDown(plot, { key: '+' });
      act(() => {
        vi.advanceTimersByTime(600);
      });
      expect(screen.getByTestId('chart-live').textContent).toMatch(/^Showing /);
    } finally {
      vi.useRealTimers();
    }
  });

  it('A14: arrow keys skip a pie part hidden from the legend; all series hidden says so', async () => {
    await renderChart({ type: 'pie', x_key: 'k', y_keys: ['v'], title: 'Split', stacked: false }, [
      { k: 'Alpha', v: 5 },
      { k: 'Beta', v: 3 },
      { k: 'Gamma', v: 1 },
    ]);
    const parts = screen.getByRole('group', { name: 'Parts' });
    fireEvent.click(within(parts).getByRole('button', { name: /Beta/ }));
    const plot = screen.getByTestId('chart-plot');
    fireEvent.keyDown(plot, { key: 'ArrowDown' });
    expect(screen.getByTestId('chart-live').textContent).toMatch(/^Alpha:/);
    fireEvent.keyDown(plot, { key: 'ArrowDown' });
    expect(screen.getByTestId('chart-live').textContent).toMatch(/^Gamma:/);
    cleanup();

    await renderChart();
    const legend = screen.getByRole('group', { name: 'Legend' });
    for (const b of within(legend).getAllByRole('button')) fireEvent.click(b);
    h.actions.length = 0;
    fireEvent.keyDown(screen.getByTestId('chart-plot'), { key: 'ArrowRight' });
    expect(screen.getByTestId('chart-live').textContent).toBe('All series hidden. Use the legend to show one.');
    expect(h.actions.find((a) => a.type === 'showTip')).toBeUndefined();
  });

  it('A19: under reduced motion even the FIRST option is not animated', async () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q.includes('reduce'),
      media: q,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    await renderChart();
    expect(h.options[0].animation).toBe(false);
  });

  it('A20: the key hint floats over the content on focus instead of pushing it down', async () => {
    await renderChart();
    const plot = screen.getByTestId('chart-plot');
    // jsdom has no keyboard modality; treat the focus as keyboard focus.
    vi.spyOn(plot, 'matches').mockImplementation(() => true);
    act(() => plot.focus());
    const hint = document.getElementById(plot.getAttribute('aria-describedby')!.split(' ')[1])!;
    expect(hint.className).toContain('chart-hint-pop');
    expect(hint.parentElement?.className).toContain('relative');
  });
});

describe('review fixes: robustness', () => {
  it.each([
    ['y_keys not a list', { type: 'bar', x_key: 'a', y_keys: 'v' } as unknown as ChartSpec, [{ a: 'x', v: 1 }]],
    ['a null row', SPEC, [null, { region: 'North', open_cases: 1, closed_cases: 2 }] as unknown as DataRow[]],
    ['data not a list', SPEC, { rows: 1 } as unknown as DataRow[]],
  ])('R1: %s never takes the conversation down', (_name, spec, data) => {
    expect(() => render(<ChartView spec={spec} data={data} />)).not.toThrow();
    expect(document.body.textContent).toMatch(/chart|Chart/);
  });

  it('R12: after the form switches to the bar list, the legend no longer drives the old canvas', async () => {
    const rows = ['Northern region head office', 'Southern region head office', 'Eastern region head office'].map((region, i) => ({
      region,
      open_cases: 10 + i,
      closed_cases: 20 - i,
    }));
    await renderChart(SPEC, rows);
    rectWidth = 200;
    act(() => roCallbacks.forEach((cb) => cb()));
    await waitFor(() => expect(document.querySelector('.chart-bars')).not.toBeNull());
    h.actions.length = 0;
    fireEvent.click(within(screen.getByRole('group', { name: 'Legend' })).getAllByRole('button')[0]);
    expect(h.actions).toEqual([]);
  });

  it('V9: a single-series bar list names its measure above the values', () => {
    const rows = Array.from({ length: 4 }, (_, i) => ({ owner: `Owner ${i + 1} of the team`, records: 10 - i }));
    render(
      <ChartView spec={{ type: 'horizontal_bar', x_key: 'owner', y_keys: ['records'], title: 'By owner', stacked: false }} data={rows} />,
    );
    expect(document.querySelector('.chart-measure')?.textContent).toBe('Records');
  });
});
