// @vitest-environment jsdom
/**
 * The safety proof for `splitMarkdown` (NEW-24).
 *
 * Segmenting a streaming answer is only allowed if rendering the pieces is
 * INDISTINGUISHABLE from rendering the whole. This does not argue that from
 * the CommonMark spec — it renders both through the real react-markdown
 * pipeline (remark-gfm + rehype-highlight, the app's own configuration) and
 * compares the HTML byte for byte.
 *
 * And it does it for every prefix of every document, because that is what
 * streaming actually produces: the splitter has to be correct not just for a
 * finished answer but at every intermediate length it passes through.
 */

import { renderToStaticMarkup } from 'react-dom/server';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { describe, expect, it } from 'vitest';
import { splitMarkdown } from '@/lib/markdownSegments';

/** The app's exact plugin configuration (see components/Markdown.tsx). */
const render = (text: string) =>
  renderToStaticMarkup(
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[[rehypeHighlight, { detect: false }]]}
    >
      {text}
    </ReactMarkdown>,
  );

/** What the segmented renderer puts on screen: frozen chunks then the tail. */
const renderSegmented = (text: string) => {
  const { frozen, tail } = splitMarkdown(text);
  return [...frozen, tail].map(render).join('');
};

/**
 * Compare two HTML strings as RENDERED TREES.
 *
 * react-markdown emits a "\n" text node between sibling block elements, and
 * concatenating separately-rendered chunks loses the one at each seam. That
 * whitespace is insignificant between blocks — HTML collapses it and nothing
 * is painted for it — but it is very significant INSIDE preformatted content,
 * where a lost newline would corrupt a code block.
 *
 * So this drops whitespace-only text nodes outside `<pre>` on BOTH sides and
 * compares everything else exactly, including every character inside a `<pre>`.
 * That is the real equivalence claim: same elements, same attributes, same
 * text, same code.
 */
function normalize(html: string): string {
  const host = document.createElement('div');
  host.innerHTML = html;
  const walk = (node: Node, inPre: boolean) => {
    for (const child of [...node.childNodes]) {
      if (child.nodeType === 3) {
        if (!inPre && (child.textContent ?? '').trim() === '') child.remove();
      } else if (child.nodeType === 1) {
        const tag = (child as Element).tagName;
        walk(child, inPre || tag === 'PRE' || tag === 'CODE');
      }
    }
  };
  walk(host, false);
  return host.innerHTML;
}

const DOCS: Record<string, string> = {
  plainProse:
    'The quick brown fox jumps over the lazy dog.\n\nA second paragraph follows here.\n\nAnd a third one.\n',
  headingsAndLists: `# Title

Intro paragraph with **bold**, *italic* and \`inline code\`.

## Section one

- alpha
- beta
- gamma

Some prose between the lists.

1. first
2. second

### Deeper

Closing words.
`,
  looseList: `Intro.

- one

- two

- three

After the list.
`,
  nestedList: `Lead in.

- outer
  - inner one
  - inner two

    a paragraph inside the item

- outer two

Done.
`,
  codeFences: `Here is some code:

\`\`\`ts
const value = 123;

// a blank line inside the fence
function go() {
  return value;
}
\`\`\`

And after it.

\`\`\`python
def f():
    return 1
\`\`\`

The end.
`,
  fenceWithTildes: `Before.

~~~js
const a = 1;

const b = 2;
~~~

After.
`,
  table: `Results below.

| Name | Value | Notes |
|---|---:|---|
| alpha | 1 | first |
| beta | 22 | second |
| gamma | 333 | third |

After the table.
`,
  blockquote: `Intro.

> a quote
> continues here

> another quote

Outro.
`,
  referenceLinks: `See [the docs][d] and [more][m].

Some prose here.

[d]: https://example.com/docs
[m]: https://example.com/more
`,
  footnotes: `Text with a footnote[^1].

More text.

[^1]: The footnote body.
`,
  htmlBlock: `Before.

<div class="x">
raw html
</div>

After.
`,
  thematicBreaks: `One.

---

Two.

***

Three.
`,
  unicode: `नमस्ते दुनिया 😊

यह एक **हिंदी** पैराग्राफ है।

- पहला
- दूसरा

Emoji: 🚀🎉 and punctuation — “quoted” … ¡olé!
`,
  citations: `Answer text [1] with citations [2].

More prose [3].

Final line.
`,
  mixed: `## Overview

Intro **prose** with a [link](https://example.com).

\`\`\`ts
const x: number = 1;
\`\`\`

| a | b |
|---|---|
| 1 | 2 |

- bullet one
- bullet two

> quoted

Done [1].
`,
  setext: `Title
=====

Body text.

Subtitle
--------

More body.
`,
  indentedCode: `Prose.

    indented code line one
    indented code line two

Back to prose.
`,
  consecutiveBlankLines: `First.



Second after several blank lines.



Third.
`,
  trailingFenceOpen: `Some prose.

\`\`\`ts
const partial = 'still streaming
`,
};

describe('splitMarkdown renders identically to the unsplit document', () => {
  for (const [name, doc] of Object.entries(DOCS)) {
    it(`${name}: every streaming prefix renders identically`, () => {
      // Every prefix, because a stream passes through all of them. Stepping by
      // one character on the shorter docs and by three on the longer ones
      // keeps this exhaustive where it matters without a minute-long test.
      const step = doc.length > 400 ? 3 : 1;
      for (let end = 0; end <= doc.length; end += step) {
        const prefix = doc.slice(0, end);
        expect(
          normalize(renderSegmented(prefix)),
          `prefix length ${end}`,
        ).toBe(normalize(render(prefix)));
      }
      expect(normalize(renderSegmented(doc))).toBe(normalize(render(doc)));
    });
  }
});

describe('splitMarkdown preserves the text exactly', () => {
  for (const [name, doc] of Object.entries(DOCS)) {
    it(`${name}: frozen + tail re-joins to the original`, () => {
      for (let end = 0; end <= doc.length; end += 1) {
        const prefix = doc.slice(0, end);
        const { frozen, tail } = splitMarkdown(prefix);
        expect([...frozen, tail].join('')).toBe(prefix);
      }
    });
  }
});

describe('splitMarkdown refuses to split what it cannot prove safe', () => {
  it('never splits a document containing link reference definitions', () => {
    expect(splitMarkdown(DOCS.referenceLinks).frozen).toEqual([]);
  });

  it('never splits a document containing footnote definitions', () => {
    expect(splitMarkdown(DOCS.footnotes).frozen).toEqual([]);
  });

  it('never splits a document containing an HTML block', () => {
    expect(splitMarkdown(DOCS.htmlBlock).frozen).toEqual([]);
  });

  it('never splits inside an open fence', () => {
    const { frozen, tail } = splitMarkdown(DOCS.trailingFenceOpen);
    expect(frozen).toEqual([]);
    expect(tail).toBe(DOCS.trailingFenceOpen);
  });

  it('never begins a chunk on a list marker, quote or indented line', () => {
    for (const doc of Object.values(DOCS)) {
      for (const chunk of splitMarkdown(doc).frozen) {
        expect(chunk).not.toMatch(/^(?: {0,3}(?:[-*+]|\d{1,9}[.)])[ \t]| {0,3}>| {4,}|\t)/);
      }
    }
  });
});

describe('splitMarkdown actually splits the common cases', () => {
  it('freezes settled blocks of an ordinary answer', () => {
    const { frozen } = splitMarkdown(DOCS.headingsAndLists);
    expect(frozen.length).toBeGreaterThan(2);
  });

  it('freezes a completed code fence', () => {
    const { frozen } = splitMarkdown(DOCS.codeFences);
    expect(frozen.some((c) => c.includes('```ts'))).toBe(true);
  });

  it('keeps the growing tail bounded as an answer gets long', () => {
    let text = '';
    for (let i = 0; i < 200; i += 1) {
      text += `## Section ${i}\n\nSome prose for section ${i} here.\n\n`;
    }
    const { frozen, tail } = splitMarkdown(text);
    expect(frozen.length).toBeGreaterThan(100);
    // The per-frame parse is now the tail alone, not the whole answer.
    expect(tail.length).toBeLessThan(200);
    expect(text.length).toBeGreaterThan(8000);
  });
});
