// @vitest-environment jsdom
/**
 * A single newline in an answer is a line break on screen.
 *
 * The owner's 2026-09-17 report: a rewrite came back as `Label: value` lines,
 * one per line in the source, and the page showed them as one run-on
 * paragraph. That is CommonMark behaving exactly as specified — a soft line
 * break folds to a space — and it is the wrong rule for a chat answer, which
 * nobody hard-wraps at 80 columns. remark-breaks makes the newline a `<br>`.
 *
 * The cases below are the two halves of that change. First, that the break
 * really appears: in a paragraph, in a blockquote, and on a list item's lazy
 * continuation line. Second — the half worth more, because it is what a
 * plugin like this can quietly destroy — that every construct whose newlines
 * carry meaning renders byte for byte as it did before: fenced code, a
 * ```mermaid block that must still reach MermaidBlock, a GFM table, inline
 * code, and the two-space hard break that was already a break.
 *
 * Everything goes through CHAT_REMARK_PLUGINS or the real `Markdown`
 * component. A local plugin list would prove something about this file.
 */

import { cleanup, render } from '@testing-library/react';
import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { afterEach, describe, expect, it } from 'vitest';

import { CHAT_REMARK_PLUGINS, Markdown } from '@/components/Markdown';

afterEach(cleanup);

/**
 * The lines a reader sees inside one block element: its text split at every
 * `<br>`, which is the only thing that puts two lines on screen inside a
 * paragraph. Asserting on this rather than on `innerHTML` says what the
 * reader gets without pinning the markup around it.
 */
function visibleLines(el: Element | null): string[] {
  expect(el).not.toBeNull();
  const lines: string[] = [''];
  const walk = (node: Node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        lines[lines.length - 1] += child.textContent ?? '';
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        const tag = (child as Element).tagName;
        if (tag === 'BR') lines.push('');
        else if (tag === 'UL' || tag === 'OL') continue;
        else walk(child);
      }
    }
  };
  walk(el as Element);
  return lines.map((l) => l.replace(/\s+/g, ' ').trim()).filter((l) => l !== '');
}

describe('a single newline breaks the line', () => {
  it('splits a paragraph of Label: value lines', () => {
    const { container } = render(
      <Markdown text={'Job Title: Data Engineer\nLocation: Austin, TX'} />,
    );
    const paragraphs = container.querySelectorAll('p');
    // One paragraph, two lines — not two paragraphs, which would add the
    // block spacing `.md > * + *` puts between real paragraphs.
    expect(paragraphs).toHaveLength(1);
    expect(paragraphs[0].querySelectorAll('br')).toHaveLength(1);
    expect(visibleLines(paragraphs[0])).toEqual([
      'Job Title: Data Engineer',
      'Location: Austin, TX',
    ]);
  });

  it('splits a two-line blockquote', () => {
    const { container } = render(
      <Markdown text={'> a quote\n> continues here\n'} />,
    );
    const quote = container.querySelector('blockquote');
    expect(quote?.querySelectorAll('br')).toHaveLength(1);
    expect(visibleLines(quote)).toEqual(['a quote', 'continues here']);
  });

  it("splits a list item's lazy continuation line", () => {
    const { container } = render(
      <Markdown text={'- item one\n  a lazy continuation line\n- item two\n'} />,
    );
    const items = container.querySelectorAll('li');
    expect(items).toHaveLength(2);
    expect(visibleLines(items[0])).toEqual(['item one', 'a lazy continuation line']);
    expect(visibleLines(items[1])).toEqual(['item two']);
  });

  it('gives a plain-lines answer one line per source line', () => {
    // The Chat B shape: section names and `Label: value` lines, single
    // newlines throughout. Before remark-breaks this was one run of text per
    // blank-line-separated group; the reader saw a wall.
    const answer = [
      'Job Title: Data Engineer',
      'Location: Austin, TX',
      'Employment Type: Full-time',
      '',
      'Responsibilities',
      'Build and own the ingestion pipelines.',
      'Keep the warehouse models documented.',
    ].join('\n');
    const { container } = render(<Markdown text={answer} />);
    const lines = [...container.querySelectorAll('p')].flatMap((p) =>
      visibleLines(p),
    );
    expect(lines).toEqual(answer.split('\n').filter((l) => l !== ''));
  });
});

/**
 * The two spaces that make a CommonMark hard break, written as an expression
 * because trailing whitespace does not survive a source file: editors and
 * lint rules strip it, and a silently stripped hard break would turn this
 * guard into a test of nothing.
 */
const HARD = '  ';

/** A document made only of constructs whose newlines are significant. */
const PROBE = `Fenced code, which must keep its own line endings:

\`\`\`python
def f():
    return 1
\`\`\`

\`\`\`mermaid
graph TD
A-->B
\`\`\`

| Name | Value |
|---|---|
| alpha | 1 |
| beta | 22 |

A paragraph with \`inline code\` in it.

A two-space hard break ends this line${HARD}
and this is the next one.

    indented code line one
    indented code line two
`;

const staticRender = (text: string, plugins: typeof CHAT_REMARK_PLUGINS) =>
  renderToStaticMarkup(
    <ReactMarkdown
      remarkPlugins={plugins}
      rehypePlugins={[[rehypeHighlight, { detect: false }]]}
    >
      {text}
    </ReactMarkdown>,
  );

describe('constructs whose newlines mean something are untouched', () => {
  it('renders the probe document identically with and without remark-breaks', () => {
    expect(staticRender(PROBE, CHAT_REMARK_PLUGINS)).toBe(
      staticRender(PROBE, [remarkGfm]),
    );
  });

  it('adds exactly one break for a two-space hard break, not two', () => {
    const { container } = render(
      <Markdown text={`first line${HARD}\nsecond line\n`} />,
    );
    expect(container.querySelectorAll('br')).toHaveLength(1);
    expect(visibleLines(container.querySelector('p'))).toEqual([
      'first line',
      'second line',
    ]);
  });

  it('keeps a code fence a single <pre> with its newlines intact', () => {
    const { container } = render(
      <Markdown text={'```python\ndef f():\n    return 1\n```\n'} />,
    );
    const pre = container.querySelector('pre');
    expect(pre?.querySelectorAll('br')).toHaveLength(0);
    expect(pre?.textContent).toBe('def f():\n    return 1\n');
    expect(container.querySelector('code')?.className).toContain(
      'language-python',
    );
  });

  it('still routes a ```mermaid block to the diagram renderer', () => {
    const { container } = render(
      <Markdown text={'```mermaid\ngraph TD\nA-->B\n```\n'} />,
    );
    // MermaidBlock labels itself; an ordinary CodeBlock prints the language.
    expect(container.textContent).toContain('Mermaid');
    expect(container.textContent).not.toContain('mermaid\ngraph');
    expect(container.querySelectorAll('br')).toHaveLength(0);
  });

  it('keeps a GFM table a table', () => {
    const { container } = render(
      <Markdown text={'| a | b |\n|---|---|\n| 1 | 2 |\n'} />,
    );
    expect(container.querySelectorAll('table')).toHaveLength(1);
    expect(container.querySelectorAll('tbody tr')).toHaveLength(1);
    expect(container.querySelectorAll('br')).toHaveLength(0);
  });
});
