// @vitest-environment jsdom
/**
 * DIAG-34 — the block draws the diagram even when the role is in the wrong
 * place, and it shows the source it actually drew.
 *
 * Why this file exists: on 2026-09-27 the orchestrator's chat prompt was
 * taught the `:::role` form (`DIAGRAM_ROLES` in
 * orchestrator/app/engines/__init__.py), because the four-colour role palette
 * this app has carried since 2026-09-22 was unreachable — measured the same
 * day in Chromium 153, every node of a five-node chat flowchart came back
 * fill rgb(51,56,61) in dark and rgb(228,231,234) in light, one grey for the
 * whole diagram.
 *
 * Teaching a syntax means it will land in the wrong diagram type sooner or
 * later, and there it is not inert, it is FATAL: measured against an esbuild
 * bundle of this component, `U:::external` in a sequenceDiagram threw
 * "Parse error on line 6 … Expecting '()', 'SOLID_OPEN_ARROW', … got 'TXT'"
 * and the block replaced a working diagram with "Couldn't render this diagram
 * — showing the source". One retry on the role-free source draws it.
 *
 * mermaid is mocked: what is under test is the block's RETRY and what it then
 * shows, not mermaid's parser. The parser's verdict is the thing the mock
 * imitates — throw on a source that still carries a role application.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/** Every source mermaid was asked to render, in order, across a test. */
const attempts: string[] = [];

const SVG =
  '<svg id="drawn" viewBox="0 0 450 267" width="450" height="267">' +
  '<text>Browser</text></svg>';

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async (_id: string, source: string) => {
      attempts.push(source);
      // What mermaid 11.17 actually does with a role outside the flowchart
      // family, and with the bare identifier that stripping only the suffix
      // would leave behind.
      if (source.includes(':::') || source.includes('STILL-BROKEN')) {
        throw new Error("Parse error on line 6: … got 'TXT'");
      }
      return { svg: SVG };
    }),
  },
}));

import { MermaidBlock } from '@/components/MermaidBlock';

const SEQUENCE_WITH_ROLE = [
  'sequenceDiagram',
  '  participant U as Browser',
  '  participant A as Orchestrator',
  '  U->>A: ask',
  '  A-->>U: answer',
  '  U:::external',
].join('\n');

beforeEach(() => {
  attempts.length = 0;
});

afterEach(() => {
  cleanup();
});

/** The source shown on the Code tab — what the copy button hands over. */
function codeTabText(): string {
  fireEvent.click(screen.getByLabelText('Show diagram source'));
  return screen.getByRole('code').textContent ?? '';
}

describe('DIAG-34 · a role in the wrong diagram type costs the colour, not the diagram', () => {
  it('DIAG-34 · a sequenceDiagram carrying a role still draws', async () => {
    render(<MermaidBlock code={SEQUENCE_WITH_ROLE} />);
    await waitFor(() => expect(document.querySelector('#drawn')).not.toBeNull());

    expect(attempts).toHaveLength(2);
    expect(attempts[0]).toContain(':::external');
    expect(attempts[1]).not.toContain(':::');
    // The retry drops the whole statement: a bare `U` is the same parse error.
    expect(attempts[1].split('\n').at(-1)).toBe('  A-->>U: answer');
    expect(screen.queryByText(/Couldn't render this diagram/)).toBeNull();
  });

  it('DIAG-34b · the Code tab shows the source that was DRAWN', async () => {
    render(<MermaidBlock code={SEQUENCE_WITH_ROLE} />);
    await waitFor(() => expect(document.querySelector('#drawn')).not.toBeNull());
    const shown = codeTabText();
    // Copying a role that was never painted would be a lie about the diagram
    // — the same rule that makes the Code tab show the SANITISED source.
    expect(shown).not.toContain(':::external');
    expect(shown).toContain('participant U as Browser');
  });

  it('DIAG-34c · a diagram that renders is never rewritten', async () => {
    render(
      <MermaidBlock
        code={'flowchart LR\n  A["Gateway"] --> B["Queue"]'}
      />,
    );
    await waitFor(() => expect(document.querySelector('#drawn')).not.toBeNull());
    expect(attempts).toHaveLength(1);
    expect(attempts[0]).toContain('classDef service');
    expect(codeTabText()).toContain('A["Gateway"] --> B["Queue"]');
  });

  it('DIAG-34d · a diagram that is broken for another reason still shows the error', async () => {
    // The retry is not a second chance for everything: when the role-free
    // source fails too, the block must keep telling the truth.
    render(
      <MermaidBlock code={'sequenceDiagram\n  A->>B: STILL-BROKEN\n  A:::service'} />,
    );
    await waitFor(() =>
      expect(screen.getByText(/Couldn't render this diagram/)).toBeTruthy(),
    );
    expect(attempts).toHaveLength(2);
    expect(document.querySelector('#drawn')).toBeNull();
  });
});
