// @vitest-environment jsdom
/**
 * Mermaid number charts in the chat (charts design v2 §10, C10/C11).
 *
 * A ```mermaid xychart / pie block becomes a real chart; a block the strict
 * parser refuses keeps the existing Mermaid path; and while the answer is
 * still streaming, a chart-headed block is a reserved "Drawing the chart…" box
 * that touches neither the parser nor mermaid. mermaid is mocked, as in the
 * other mermaid-block tests. Made-up data.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({ mermaidRenders: 0, parses: 0 }));

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async () => {
      h.mermaidRenders += 1;
      return { svg: '<svg viewBox="0 0 10 10" width="10" height="10"></svg>' };
    }),
  },
}));

vi.mock('@/lib/mermaidChart', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/lib/mermaidChart')>();
  return {
    ...real,
    parseMermaidChart: (code: string) => {
      h.parses += 1;
      return real.parseMermaidChart(code);
    },
  };
});

vi.mock('@/components/EChart', () => ({
  default: (props: { height: number }) => <div data-testid="echart-stub" style={{ height: props.height }} />,
}));

import { MermaidBlock } from '@/components/MermaidBlock';
import { ChartStreamingContext } from '@/components/chart/ChartStreamingContext';

const XY = [
  'xychart-beta',
  '  title "Applications by month"',
  '  x-axis [Jan, Feb, Mar]',
  '  y-axis "Count"',
  '  bar [12, 30, 18]',
].join('\n');

beforeEach(() => {
  h.mermaidRenders = 0;
  h.parses = 0;
});

afterEach(() => {
  cleanup();
});

describe('converted charts', () => {
  it('renders an xychart as a chart block with a Chart/Code toggle and copy', async () => {
    render(<MermaidBlock code={XY} />);
    const block = screen.getByTestId('mermaid-chart');
    expect(block.textContent).toContain('Chart');
    expect(block.textContent).not.toContain('Mermaid');
    expect(screen.getByText('Applications by month')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Copy chart source' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Show chart source' }));
    expect(block.querySelector('pre code')?.textContent).toBe(XY);
    fireEvent.click(screen.getByRole('button', { name: 'Show chart' }));
    await screen.findByTestId('echart-stub');
    expect(h.mermaidRenders).toBe(0);
  });

  it('draws the 26-name complaint block as a bar list with every name in full', () => {
    const names = Array.from({ length: 26 }, (_, i) => `Team member number ${i + 1} (North)`);
    const code = `xychart-beta\n x-axis [${names.join(', ')}]\n y-axis "Count" 0 --> 3500\n bar [${names
      .map((_, i) => 3000 - i * 90)
      .join(', ')}]`;
    render(<MermaidBlock code={code} />);
    const labels = Array.from(document.querySelectorAll('.chart-bar-label')).map((l) => l.textContent);
    expect(labels.filter(Boolean)).toEqual(names);
    expect(h.mermaidRenders).toBe(0);
  });

  it('falls back to the existing Mermaid path for a block the parser refuses', async () => {
    render(<MermaidBlock code={'xychart-beta\n x-axis [a, b]\n bar [1, two]'} />);
    expect(screen.queryByTestId('mermaid-chart')).toBeNull();
    expect(screen.getByText('Mermaid')).toBeTruthy();
    await waitFor(() => expect(h.mermaidRenders).toBeGreaterThan(0));
  });

  it('leaves other diagrams alone', async () => {
    render(<MermaidBlock code={'flowchart TD\n A --> B'} />);
    expect(screen.getByText('Mermaid')).toBeTruthy();
    expect(h.parses).toBe(0);
  });
});

describe('while streaming', () => {
  it('shows "Drawing the chart…" for a pie prefix, without parsing or loading mermaid', async () => {
    const prefix = 'pie title Share\n "Applied" : 30\n';
    const { rerender } = render(
      <ChartStreamingContext.Provider value={true}>
        <MermaidBlock code={prefix} />
      </ChartStreamingContext.Provider>,
    );
    expect(screen.getByText('Drawing the chart…')).toBeTruthy();
    rerender(
      <ChartStreamingContext.Provider value={true}>
        <MermaidBlock code={`${prefix} "Placed" : 70\n`} />
      </ChartStreamingContext.Provider>,
    );
    expect(screen.getByText('Drawing the chart…')).toBeTruthy();
    expect(h.parses).toBe(0);
    expect(h.mermaidRenders).toBe(0);
    expect(screen.queryByTestId('mermaid-chart')).toBeNull();

    await act(async () => {
      rerender(
        <ChartStreamingContext.Provider value={false}>
          <MermaidBlock code={`${prefix} "Placed" : 70\n`} />
        </ChartStreamingContext.Provider>,
      );
    });
    expect(screen.queryByText('Drawing the chart…')).toBeNull();
    expect(screen.getByTestId('mermaid-chart')).toBeTruthy();
    expect(h.parses).toBeGreaterThan(0);
    expect(h.mermaidRenders).toBe(0);
  });
});

describe('review fixes (2026-11-25)', () => {
  it('A18: the Chart / Code toggles are full-size chart buttons (44 px on touch)', async () => {
    render(<MermaidBlock code={XY} />);
    await screen.findByTestId('mermaid-chart');
    expect(screen.getByRole('button', { name: 'Show chart source' }).className).toContain('chart-btn');
    expect(screen.getByRole('button', { name: 'Show chart' }).className).toContain('chart-btn');
  });

  it('V17: while streaming, a long x list reserves about the bar list height', () => {
    const long = `xychart-beta\n x-axis [${Array.from({ length: 26 }, (_, i) => `N${i}`).join(', ')}]\n bar [`;
    render(
      <ChartStreamingContext.Provider value={true}>
        <MermaidBlock code={long} />
      </ChartStreamingContext.Provider>,
    );
    expect(screen.getByRole('status').style.height).toBe('min(520px, 60vh)');
  });
});
