// @vitest-environment jsdom
/**
 * Where chat charts sit (charts design v2 §5.2, §10): the answer chart has no
 * fixed 444 px reservation (the figure reserves its own planned height), the
 * proof drawer's chart says it is in the drawer, and MessageRow tells Mermaid
 * blocks whether the answer is still streaming.
 *
 * Markdown is replaced by a stub that renders its text as one MermaidBlock, so
 * this file needs nothing from the markdown pipeline. Made-up data.
 */
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/components/Markdown', async () => {
  const { MermaidBlock } = await import('@/components/MermaidBlock');
  return { Markdown: ({ text }: { text: string }) => <MermaidBlock code={text} /> };
});

vi.mock('@/components/EChart', () => ({
  default: (props: { height: number }) => <div data-testid="echart-stub" style={{ height: props.height }} />,
}));

vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: '<svg></svg>' })) },
}));

import { MessageRow } from '@/components/MessageRow';
import type { ChatMessage, Meta } from '@/lib/types';

afterEach(cleanup);

function row(message: ChatMessage) {
  return (
    <MessageRow
      message={message}
      isLast
      onRegenerate={vi.fn()}
      onRetry={vi.fn()}
      onEditStart={vi.fn()}
      onEditCancel={vi.fn()}
      onEditSubmit={vi.fn()}
    />
  );
}

const META = {
  engine: 'sf_assistant',
  route: 'sql',
  chart: { type: 'bar', x_key: 'stage', y_keys: ['n'], title: 'Cases by stage', stacked: false },
  data: [
    { stage: 'Open', n: 4 },
    { stage: 'Closed', n: 9 },
  ],
} as unknown as Meta;

describe('answer chart', () => {
  it('has no fixed min-height and draws the inline figure', async () => {
    render(row({ id: 'a', role: 'assistant', content: 'Two stages.', status: 'done', createdAt: 0, meta: META }));
    const box = screen.getByTestId('answer-chart');
    expect(box.style.minHeight).toBe('');
    expect(box.querySelector('figure[data-context="inline"]')).not.toBeNull();
    await screen.findAllByTestId('echart-stub');
  });

  it('the proof drawer chart (non-sf_assistant engines) is marked as the drawer context', async () => {
    const meta = { ...META, engine: 'sql' } as unknown as Meta;
    render(row({ id: 'a', role: 'assistant', content: 'Two stages.', status: 'done', createdAt: 0, meta }));
    expect(screen.queryByTestId('answer-chart')).toBeNull();
    // The drawer opens its Chart section by default when it holds one.
    expect(document.querySelector('button[aria-controls="drawer-chart"]')?.getAttribute('aria-expanded')).toBe('true');
    expect(document.querySelector('figure[data-context="drawer"]')).not.toBeNull();
  });
});

describe('streaming context', () => {
  const PIE = 'pie title Split\n "A" : 30\n "B" : 70\n';

  it('a streaming answer holds a Mermaid pie as "Drawing the chart…" until it settles', async () => {
    const base = { id: 'p', role: 'assistant' as const, content: PIE, createdAt: 0 };
    const { rerender } = render(row({ ...base, status: 'streaming' }));
    expect(screen.getByText('Drawing the chart…')).toBeTruthy();
    await act(async () => {
      rerender(row({ ...base, status: 'done' }));
    });
    expect(screen.queryByText('Drawing the chart…')).toBeNull();
    expect(screen.getByTestId('mermaid-chart')).toBeTruthy();
  });
});
