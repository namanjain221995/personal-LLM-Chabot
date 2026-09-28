// @vitest-environment jsdom
/**
 * A diagram does not flicker while it is being written.
 *
 * MEASURED IN A REAL BROWSER, 2026-09-28, on a 30-node flowchart streamed at
 * production speed: the block flipped between the diagram and the red
 * "Couldn't render this diagram" notice 58 times in one 5.3 s stream (max 74),
 * and was showing that FALSE error on 176 of 296 sampled frames — 59% of the
 * time the diagram was being written. It rendered and threw away 30 diagrams,
 * and its own height swung between 257 px and 3,143 px, pushing the rest of
 * the answer up and down by thousands of pixels, dozens of times. Total
 * Blocking Time p50 446 ms; about 16 fps.
 *
 * The cause was two lines of this component. `code` changes on every streamed
 * token and the render effect depends on it, so every token ran a full
 * `mermaid.render()`; and the catch wiped the SVG and set the error, so a
 * token landing mid-label — `N1["Step 1 of the pipe` — replaced a working
 * diagram with a failure notice.
 *
 * Nothing tested MermaidBlock with a GROWING `code` prop. The existing
 * frontend tests assert `looksRenderable` on fixed strings, which is why a
 * defect this loud survived in production.
 *
 * mermaid is mocked: what is under test is WHEN this component renders and
 * what it shows while the source is incomplete, not mermaid's parser. The
 * mock imitates the parser's one relevant verdict — an unterminated label
 * throws.
 */
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/** Every source mermaid was asked to render, in order. */
const attempts: string[] = [];

const SVG = '<svg id="drawn" viewBox="0 0 400 300" width="400" height="300"><text>ok</text></svg>';

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async (_id: string, source: string) => {
      attempts.push(source);
      // mermaid 11.17's verdict on a half-written label: an opening `["` with
      // no closing `"]` on the same line is a parse error.
      for (const line of source.split('\n')) {
        const opens = (line.match(/\["/g) || []).length;
        const closes = (line.match(/"\]/g) || []).length;
        if (opens !== closes) throw new Error('Parse error: unterminated label');
      }
      return { svg: SVG };
    }),
  },
}));

import { MermaidBlock } from '@/components/MermaidBlock';

/** The frames a real stream produces: the label arrives a piece at a time. */
const FRAMES = [
  'flowchart TD\n  A["Start"] --> B["Next"]',
  'flowchart TD\n  A["Start"] --> B["Next"]\n  B --> C["Step 1 of the pipe',
  'flowchart TD\n  A["Start"] --> B["Next"]\n  B --> C["Step 1 of the pipeline"]',
  'flowchart TD\n  A["Start"] --> B["Next"]\n  B --> C["Step 1 of the pipeline"]\n  C --> D["Don',
  'flowchart TD\n  A["Start"] --> B["Next"]\n  B --> C["Step 1 of the pipeline"]\n  C --> D["Done"]',
];

const errorShown = () => screen.queryByText(/Couldn't render this diagram/i) !== null;

beforeEach(() => {
  attempts.length = 0;
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

/**
 * Let the trailing render fire and its promise resolve: the source has STOPPED
 * growing.
 */
async function settle(ms = 400) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
    await Promise.resolve();
    await Promise.resolve();
  });
}

/**
 * One token of a real stream: the next frame arrives well inside the debounce
 * window, so the source has NOT stopped growing.
 *
 * This distinction is the whole test. An earlier version of this file advanced
 * 400 ms between frames and then asserted that a broken intermediate frame
 * kept the last good diagram — but a 400 ms gap IS the source settling, and a
 * settled broken source is exactly the case that SHOULD report. Tokens arrive
 * a few milliseconds apart; modelling them as pauses tested a stream nobody
 * has.
 */
async function token(ms = 20) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
    await Promise.resolve();
  });
}

describe('a diagram being written does not flash a failure', () => {
  it('never shows the error notice for a source that is merely unfinished', async () => {
    const { rerender } = render(<MermaidBlock code={FRAMES[0]} />);
    await settle();
    await waitFor(() => expect(document.querySelector('#drawn')).not.toBeNull());

    const seen: boolean[] = [];
    for (const frame of FRAMES.slice(1)) {
      rerender(<MermaidBlock code={frame} />);
      await token();
      seen.push(errorShown());
    }
    await settle();
    expect(seen.filter(Boolean)).toHaveLength(0);
    expect(errorShown()).toBe(false);
  });

  it('keeps the last good diagram on screen across a broken frame', async () => {
    const { rerender } = render(<MermaidBlock code={FRAMES[0]} />);
    await settle();
    await waitFor(() => expect(document.querySelector('#drawn')).not.toBeNull());

    // FRAMES[1] cannot parse, and it is mid-stream. The picture must not
    // disappear and no error may appear.
    rerender(<MermaidBlock code={FRAMES[1]} />);
    await token();
    expect(document.querySelector('#drawn')).not.toBeNull();
    expect(errorShown()).toBe(false);
    // And it survives the rest of the burst, right up to the finished source.
    for (const frame of FRAMES.slice(2)) {
      rerender(<MermaidBlock code={frame} />);
      await token();
      expect(document.querySelector('#drawn')).not.toBeNull();
      expect(errorShown()).toBe(false);
    }
  });

  it('draws the finished diagram once the source stops growing', async () => {
    const { rerender } = render(<MermaidBlock code={FRAMES[0]} />);
    await settle();
    for (const frame of FRAMES.slice(1)) {
      rerender(<MermaidBlock code={frame} />);
      await token();
    }
    await settle();
    expect(document.querySelector('#drawn')).not.toBeNull();
    expect(attempts.at(-1)).toContain('Done');
    expect(errorShown()).toBe(false);
  });

  it('renders far fewer times than there are frames', async () => {
    // The browser measurement counted 30 renders thrown away in one stream.
    // A burst that settles once should cost ONE render, not one per frame.
    const { rerender } = render(<MermaidBlock code={FRAMES[0]} />);
    await settle();
    const afterFirst = attempts.length;

    for (const frame of FRAMES.slice(1)) {
      rerender(<MermaidBlock code={frame} />);
      await token();
    }
    await settle();
    const duringBurst = attempts.length - afterFirst;
    expect(duringBurst).toBeLessThanOrEqual(2);
  });

  it('still reports a diagram that is genuinely broken when it stops growing', async () => {
    // THE OTHER HALF OF THE RULE. Suppressing mid-stream failures must not
    // suppress a real one: the trailing render is allowed to report.
    const broken = 'flowchart TD\n  A["never closed --> B["x"]';
    render(<MermaidBlock code={broken} />);
    await settle();
    await waitFor(() => expect(errorShown()).toBe(true));
  });
});
