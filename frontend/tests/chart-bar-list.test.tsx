// @vitest-environment jsdom
/**
 * The HTML bar list (charts design v2 §4.2, C2): every horizontal-bar form.
 *
 * The owner's complaint was a bar chart whose long names overlapped into an
 * unreadable band. Here every name is real text in full, each row as tall as
 * its own label, and a long list scrolls in a capped viewport with Show all.
 * ECharts must never load for it. Made-up data.
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';

const flags = vi.hoisted(() => ({ echartsImported: false }));

vi.mock('@/components/EChart', () => {
  flags.echartsImported = true;
  return { default: () => null };
});

import { ChartView } from '@/components/ChartView';
import type { ChartSpec, DataRow } from '@/lib/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const LONG =
  'Regional recruitment coordination office for the north-eastern territories and the islands, ' +
  'including temporary placements, contract renewals and the quarterly compliance review team (East)';

function ranking(n: number): DataRow[] {
  return Array.from({ length: n }, (_, i) => ({ owner: `Owner ${i + 1} of the team`, records: 3091 - i * 97 }));
}

const SPEC: ChartSpec = { type: 'horizontal_bar', x_key: 'owner', y_keys: ['records'], title: 'Records by owner', stacked: false };

function rows() {
  return Array.from(document.querySelectorAll('.chart-bars > li:not(.chart-bars-ticks)'));
}

/** Text a sighted reader sees: everything but the screen-reader-only spans. */
function seen(el: Element | null | undefined): string {
  if (!el) return '';
  const copy = el.cloneNode(true) as Element;
  copy.querySelectorAll('.sr-only').forEach((n) => n.remove());
  return copy.textContent ?? '';
}

describe('labels and values', () => {
  it('renders every label in full, with dir="auto", and values in text ink', () => {
    const data = [{ owner: LONG, records: 12 }, ...ranking(3)];
    render(<ChartView spec={SPEC} data={data} />);
    const label = rows()[0].querySelector('.chart-bar-label')!;
    expect(label.getAttribute('dir')).toBe('auto');
    expect(label.textContent).toBe(LONG);
    expect(rows()).toHaveLength(4);
    expect(rows()[1].querySelector('.chart-bar-values')?.textContent).toBe((3091).toLocaleString());
  });

  it('grows negative bars left of the zero line', () => {
    render(
      <ChartView
        spec={SPEC}
        data={[
          { owner: 'Gain', records: 40 },
          { owner: 'Loss', records: -20 },
        ]}
      />,
    );
    const [gain, loss] = rows().map((r) => r.querySelector<HTMLElement>('.chart-bar')!);
    const zero = rows()[0].querySelector<HTMLElement>('.chart-bar-zero')!;
    expect(loss.dataset.neg).toBe('true');
    expect(parseFloat(loss.style.left)).toBeLessThan(parseFloat(zero.style.left));
    expect(parseFloat(gain.style.left)).toBeCloseTo(parseFloat(zero.style.left), 5);
  });

  it('shows a stacked row total and the segments in a popover', () => {
    render(
      <ChartView
        spec={{ ...SPEC, y_keys: ['won', 'lost'], stacked: true }}
        data={[
          { owner: 'Ann', won: 3, lost: 2 },
          { owner: 'Bo', won: 1, lost: 4 },
        ]}
      />,
    );
    const first = rows()[0];
    expect(seen(first.querySelector('.chart-bar-values'))).toBe('5');
    // Review A4: a screen reader hears the segments, not only the total.
    expect(first.querySelector('.chart-bar-values')?.textContent).toBe('Won 3, Lost 2, total 5');
    expect(first.querySelectorAll('.chart-bar')).toHaveLength(2);
    fireEvent.mouseEnter(first);
    const pop = first.querySelector('.chart-bar-popover')!;
    expect(pop.textContent).toContain('Won');
    expect(pop.textContent).toContain('Lost');
  });

  it('prints one value per series for grouped bars', () => {
    render(
      <ChartView
        spec={{ ...SPEC, y_keys: ['won', 'lost'] }}
        data={[{ owner: 'Ann', won: 3, lost: 2 }]}
      />,
    );
    const values = rows()[0].querySelectorAll('.chart-bar-values > span');
    expect(Array.from(values).map((v) => seen(v))).toEqual(['3', '2']);
    // Review A4: each number carries its series name for screen readers, and
    // a colour key for the eye.
    expect(Array.from(values).map((v) => v.textContent)).toEqual(['Won 3', 'Lost 2']);
    expect(rows()[0].querySelectorAll('.chart-bar-values .chart-value-swatch')).toHaveLength(2);
  });

  it('shows pie shares when a 9-part pie is drawn as bars, largest first', () => {
    const data = Array.from({ length: 9 }, (_, i) => ({ owner: `Part ${i + 1}`, records: i + 1 }));
    render(<ChartView spec={{ ...SPEC, type: 'pie' }} data={data} />);
    expect(screen.getByText(/9 parts are easier to compare as bars/)).toBeTruthy();
    expect(document.querySelector('ol.chart-bars')).not.toBeNull();
    expect(rows()[0].textContent).toContain('Part 9');
    expect(rows()[0].querySelector('.chart-bar-values')?.textContent).toBe('9 · 20.0%');
  });
});

describe('long lists', () => {
  it('caps the viewport, says so, and offers Show all / Show fewer', () => {
    render(<ChartView spec={SPEC} data={ranking(26)} />);
    expect(screen.getByText('26 rows · scroll for more')).toBeTruthy();
    const viewport = document.querySelector<HTMLElement>('.chart-bars-viewport')!;
    expect(viewport.style.maxHeight).toBe('520px');
    // Review A10: a named group (not a landmark per chart), described by the
    // summary and the key hint.
    expect(viewport.getAttribute('role')).toBe('group');
    expect(viewport.getAttribute('aria-roledescription')).toBe('bar chart');
    const [summaryId, hintId] = viewport.getAttribute('aria-describedby')!.split(' ');
    expect(document.getElementById(summaryId)?.textContent).toMatch(/^Bar chart of /);
    expect(document.getElementById(hintId)?.textContent).toMatch(/Up and down arrows read the rows/);
    expect(viewport.tabIndex).toBe(0);
    fireEvent.click(screen.getByRole('button', { name: 'Show all 26' }));
    expect(viewport.style.maxHeight).toBe('');
    fireEvent.click(screen.getByRole('button', { name: 'Show fewer' }));
    expect(viewport.style.maxHeight).toBe('520px');
  });

  it('moves a highlighted row with ↑/↓ and leaves other keys alone', () => {
    render(<ChartView spec={SPEC} data={ranking(5)} />);
    const viewport = document.querySelector<HTMLElement>('.chart-bars-viewport')!;
    fireEvent.keyDown(viewport, { key: 'ArrowDown' });
    fireEvent.keyDown(viewport, { key: 'ArrowDown' });
    expect(rows()[1].getAttribute('data-active')).toBe('true');
    expect(fireEvent.keyDown(viewport, { key: 'Tab' })).toBe(true);
    expect(fireEvent.keyDown(viewport, { key: 'PageDown' })).toBe(true);
  });
});

describe('legend, motion, layout', () => {
  it('a legend toggle hides that series in the list', () => {
    render(
      <ChartView
        spec={{ ...SPEC, y_keys: ['won', 'lost'] }}
        data={[{ owner: 'Ann', won: 3, lost: 2 }]}
      />,
    );
    expect(rows()[0].querySelectorAll('.chart-bar')).toHaveLength(2);
    const legend = screen.getByRole('group', { name: 'Legend' });
    fireEvent.click(within(legend).getByRole('button', { name: /Lost/ }));
    expect(rows()[0].querySelectorAll('.chart-bar')).toHaveLength(1);
  });

  it('grows bars with scaleX, and not at all under reduced motion', () => {
    const { unmount } = render(<ChartView spec={SPEC} data={ranking(2)} />);
    expect(document.querySelector('.chart-bar-grow')).not.toBeNull();
    unmount();
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q.includes('reduce'),
      media: q,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }));
    render(<ChartView spec={SPEC} data={ranking(2)} />);
    expect(document.querySelector('.chart-bar')).not.toBeNull();
    expect(document.querySelector('.chart-bar-grow')).toBeNull();
  });

  it('puts the label on its own line below 480 px (container query)', () => {
    const css = readFileSync(resolve(process.cwd(), 'app/globals.css'), 'utf8');
    const block = css.slice(css.indexOf('@container (max-width: 479px)'));
    expect(block).toMatch(/\.chart-bar-label\s*\{\s*grid-column:\s*1 \/ -1/);
    expect(css).toMatch(/\.chart-figure\s*\{\s*container-type:\s*inline-size/);
  });

  it('never imports ECharts for a bar list', () => {
    render(<ChartView spec={SPEC} data={ranking(30)} />);
    expect(flags.echartsImported).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Review fixes (2026-11-25). Made-up data.
// ---------------------------------------------------------------------------

import { fitTicks } from '@/components/chart/ChartBarList';
import { estimateWidth, measurerFor } from '@/lib/chartText';
import { axisTickFormatter } from '@/lib/chartFormat';

describe('review fixes: the shared scale ticks (V3)', () => {
  const measureEst = (t: string) => estimateWidth(t);
  function boxes(min: number, max: number, track: number) {
    const { ticks, label } = fitTicks(min, max, track, measureEst);
    const lo = Math.min(min, ticks[0]);
    const hi = Math.max(max, ticks[ticks.length - 1]);
    return ticks.map((t, k) => {
      const x = ((t - lo) / (hi - lo)) * track;
      const w = estimateWidth(label(t)) * (11 / 12);
      const b = k === 0 ? [x, x + w] : k === ticks.length - 1 ? [x - w, x] : [x - w / 2, x + w / 2];
      return { text: label(t), x0: b[0], x1: b[1] };
    });
  }

  it.each([
    [188, 1_060_000],
    [188, 95_000],
    [240, 900_000_000],
    [120, 1_500_000],
  ])('no two tick labels touch on a %i px track up to %i', (track, max) => {
    const b = boxes(0, max, track);
    expect(b.length).toBeGreaterThanOrEqual(2);
    for (let k = 1; k < b.length; k++) expect(b[k].x0 - b[k - 1].x1).toBeGreaterThanOrEqual(8);
    expect(b[0].x0).toBeGreaterThanOrEqual(0);
    expect(b[b.length - 1].x1).toBeLessThanOrEqual(track + 0.01);
  });

  it.each([1_200_000, 1_400_000, 1_700_000, 2_300_000, 12_500_000, 140_000, 1_300_000_000])(
    'every tick label reads back as the value it marks, up to %i (N1)',
    (max) => {
      const mult = { '': 1, k: 1e3, M: 1e6, B: 1e9, T: 1e12 } as const;
      for (const track of [118, 200, 300, 420, 640, 900]) {
        const { ticks, label } = fitTicks(0, max, track, measureEst);
        for (const t of ticks) {
          const m = /^(-?[\d.]+)([kMBT]?)$/.exec(label(t));
          expect(m, `tick ${t} printed as "${label(t)}"`).not.toBeNull();
          const shown = Number(m![1]) * mult[m![2] as keyof typeof mult];
          expect(Math.abs(shown - t), `tick ${t} printed as "${label(t)}" on a ${track} px track`).toBeLessThanOrEqual(
            1e-9 * Math.max(1, t),
          );
        }
      }
    },
  );

  it('prints compact numbers once the scale reaches 10k', () => {
    expect(boxes(0, 1_060_000, 188).map((b) => b.text)).toContain('1M');
  });

  it('a grouped bar list at phone width uses the measured track', () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(
      () => ({ width: 188, height: 20, top: 0, left: 0, right: 188, bottom: 20, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect,
    );
    render(
      <ChartView
        spec={{ type: 'horizontal_bar', x_key: 'owner', y_keys: ['won', 'lost'], title: 'T', stacked: false }}
        data={[
          { owner: 'Ann', won: 1_060_000, lost: 200_000 },
          { owner: 'Bo', won: 500_000, lost: 900_000 },
        ]}
      />,
    );
    const ticks = Array.from(document.querySelectorAll('.chart-tick')).map((t) => t.textContent);
    // Compact labels, as many as fit (the geometry is checked above).
    expect(ticks.length).toBeGreaterThanOrEqual(2);
    expect(ticks).toEqual(fitTicks(0, 1_060_000, 188, measurerFor()).ticks.map((t) => axisTickFormatter(1_060_000)(t)));
    expect(ticks.every((t) => !/,\d{3}/.test(t ?? ''))).toBe(true);
    vi.restoreAllMocks();
  });
});

describe('review fixes: bar list CSS', () => {
  const css = readFileSync(resolve(__dirname, '../app/globals.css'), 'utf8');
  const block = (selectorStart: string) => {
    const i = css.indexOf(selectorStart);
    return i < 0 ? '' : css.slice(i, css.indexOf('}', i) + 1);
  };

  it('V6/A8: only the other rows’ bars recede; labels, values and ticks keep full ink', () => {
    expect(css).not.toMatch(/li:not\(\[data-active='true'\]\) > \* \{\s*opacity/);
    expect(css).toMatch(/li:not\(\[data-active='true'\]\):not\(\.chart-bars-ticks\) > \.chart-bar-track \{\s*opacity: 0\.45/);
  });

  it('V7/A5: the row popover is an overlay that never takes a grid row', () => {
    const pop = block('.chart-bar-popover {');
    expect(pop).toMatch(/position: absolute/);
    expect(pop).not.toMatch(/grid-column/);
    expect(pop).toMatch(/pointer-events: none/);
    expect(block('.chart-bars > li {')).toMatch(/position: relative/);
  });

  it('A6: forced colours keep bars and colour keys visible', () => {
    expect(css).toMatch(/@media \(forced-colors: active\) \{[\s\S]*?\.chart-bar,[\s\S]*?forced-color-adjust: none;[\s\S]*?outline: 1px solid CanvasText/);
  });

  it('A9: a hidden legend item dims its name and swatch only', () => {
    expect(css).not.toMatch(/\.chart-legend-item\[aria-pressed='false'\] \{\s*opacity/);
    expect(css).toMatch(/\.chart-legend-item\[aria-pressed='false'\] \.chart-legend-name,/);
  });

  it('V14: the sticky tick row wears the real ground (the drawer is darker / lighter)', () => {
    expect(block('.chart-bars > li.chart-bars-ticks {')).toMatch(/background: var\(--chart-ground\)/);
    expect(css).toMatch(/\.chart-figure\[data-context='drawer'\] \{\s*--chart-ground: var\(--ts-chart-ground-drawer\)/);
  });

  it('V15: the no-subgrid fallback follows the narrow layout', () => {
    const narrow = css.slice(css.indexOf('@container (max-width: 479px)'));
    expect(narrow.slice(0, 900)).toMatch(/@supports not \(grid-template-columns: subgrid\)/);
  });

  it('A17: colour keys on hover and popover grounds carry a 1 px ink ring', () => {
    expect(css).toMatch(/\.chart-swatch,\s*\.chart-popover-dot,\s*\.chart-value-swatch \{\s*box-shadow: 0 0 0 1px color-mix\(in srgb, var\(--ts-text\) 50%, transparent\)/);
  });

  it('A18: 8 px between touch targets in the toolbar', () => {
    expect(css).toMatch(/@media \(pointer: coarse\) \{[\s\S]*?\.chart-toolbar \{\s*gap: 8px/);
  });

  it('V8: a phone-width list is capped at 60% of the screen', () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(
      () => ({ width: 317, height: 400, top: 0, left: 0, right: 317, bottom: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect,
    );
    render(<ChartView spec={SPEC} data={ranking(26)} />);
    expect(document.querySelector<HTMLElement>('.chart-bars-viewport')!.style.maxHeight).toBe('min(480px, 60vh)');
    vi.restoreAllMocks();
  });
});
