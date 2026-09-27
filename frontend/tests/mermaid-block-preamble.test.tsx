// @vitest-environment jsdom
/**
 * DIAG-36 — what the block does with a preamble, and what it does when it
 * refuses to draw at all.
 *
 * The colour ban is enforced by `guardDiagramSource`, which strips both of
 * mermaid's in-source config channels before the renderer sees them. This file
 * pins the two things the COMPONENT owes on top of that:
 *
 *  1. mermaid is handed the guarded source — no `config:` block, no
 *     `%%{init}%%` — and the Code tab shows that same string, so what a person
 *     copies is what was drawn.
 *  2. a REFUSED source never reaches mermaid at all, and says so. Refusal is
 *     reserved for the one construct that cannot be stripped without guessing
 *     (`}%%` inside a directive's own string value, which leaves a fragment in
 *     the middle of a statement). Both ways of stripping that can put a
 *     DIFFERENT graph on screen than the source describes, and a wrong picture
 *     is worse than no picture — a verifier caught exactly that failure
 *     elsewhere in this release.
 *
 * mermaid is mocked because what is under test is which STRING the component
 * hands it, not what mermaid draws. The colours themselves were measured in
 * Chromium 153.0.8010.36 against mermaid 11.17.0 with an esbuild bundle of
 * this component; those numbers are in DIAG-35's header.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/** Every source mermaid was asked to render, in order, across a test. */
const attempts: string[] = [];

const SVG = '<svg id="drawn" viewBox="0 0 450 267" width="450" height="267"><text>A</text></svg>';

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async (_id: string, source: string) => {
      attempts.push(source);
      return { svg: SVG };
    }),
  },
}));

import { MermaidBlock } from '@/components/MermaidBlock';

const FLOW = 'flowchart TD\n  SVC[Gateway]:::service --> PLAIN[Store]';

beforeEach(() => {
  attempts.length = 0;
});
afterEach(cleanup);

describe('DIAG-36 · the block hands mermaid the guarded source', () => {
  it.each([
    ['themeCSS', '---\nconfig:\n  themeCSS: ".node rect { fill: #ff0000 !important }"\n---\n'],
    ['htmlLabels', '---\nconfig:\n  htmlLabels: true\n---\n'],
    ['look', '---\nconfig:\n  look: handDrawn\n---\n'],
    ['an init directive', "%%{init: {'theme':'default'}}%%\n"],
  ])('DIAG-36 · a %s preamble never reaches mermaid', async (_name, preamble) => {
    render(<MermaidBlock code={preamble + FLOW} />);
    await waitFor(() => expect(attempts.length).toBe(1));
    expect(attempts[0]).not.toContain('config');
    expect(attempts[0]).not.toContain('%%{');
    expect(attempts[0]).not.toContain('#ff0000');
    // …and the diagram it does get is the author's graph, in our theme.
    expect(attempts[0]).toContain('SVC[Gateway]:::service --> PLAIN[Store]');
    expect(attempts[0]).toContain('classDef service');
  });

  it('DIAG-36b · a `title:` preamble is passed through', async () => {
    render(<MermaidBlock code={`---\ntitle: Request path\n---\n${FLOW}`} />);
    await waitFor(() => expect(attempts.length).toBe(1));
    expect(attempts[0]).toContain('title: Request path');
    expect(attempts[0]).toContain('classDef service');
  });

  it('DIAG-36c · a refused source is never rendered, and says why', async () => {
    const src = '%%{init: {"themeCSS": "a}%% .node rect{fill:#ff0000}"}}%%\n' + FLOW;
    render(<MermaidBlock code={src} />);
    await waitFor(() =>
      expect(screen.getByText(/This diagram was not drawn/)).toBeTruthy(),
    );
    expect(screen.getByText(/malformed/)).toBeTruthy();
    // mermaid was never asked.
    expect(attempts).toEqual([]);
    // The Preview toggle is dead, so there is no way to reach a drawing.
    expect(screen.getByLabelText('Show rendered diagram')).toHaveProperty('disabled', true);
  });

  it('DIAG-36d · a refused source is not dressed up with our classDefs', async () => {
    const src = '%%{init: {"themeCSS": "a}%% x"}}%%\n' + FLOW;
    render(<MermaidBlock code={src} />);
    await waitFor(() =>
      expect(screen.getByText(/This diagram was not drawn/)).toBeTruthy(),
    );
    const shown = screen.getByRole('code').textContent ?? '';
    expect(shown).not.toContain('classDef service');
  });

  it('DIAG-36e · an ordinary diagram is unaffected by any of this', async () => {
    render(<MermaidBlock code={FLOW} />);
    await waitFor(() => expect(attempts.length).toBe(1));
    expect(screen.queryByText(/This diagram was not drawn/)).toBeNull();
    expect(attempts[0].startsWith(FLOW)).toBe(true);
  });
});
