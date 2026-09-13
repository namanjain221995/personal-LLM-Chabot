/**
 * The "On this page" headings, read from the SAME markdown parse the renderer
 * uses — not from a regex over the source lines.
 *
 * WHY THIS REPLACED content/docs/headings.ts#headingsOf (2026-09-13, wave-3
 * re-verify). The rail slugged a regex-stripped copy of the source line while
 * the DOM slugged the text react-markdown actually rendered. Each fix taught
 * the regex one more piece of markdown (links, then emphasis) and each left
 * another out: `## A &amp; B` rendered `A & B` (id `a-b`) but listed
 * `a-amp-b`; `## See [the ref][r]` rendered `See the ref` (id `see-the-ref`)
 * but listed `see-the-ref-r`. A contents link that scrolls nowhere, invisible
 * to everything except a click. Chasing markdown with regexes cannot end, so
 * this walks the mdast that remark-parse + remark-gfm produce — the very tree
 * react-markdown turns into the DOM — and reduces each heading to the text the
 * browser will show. Entities are decoded, references resolved (and an
 * unresolved one stays literal text, on both sides), escapes applied, and
 * fenced code never yields a heading, all by the parser rather than by us.
 *
 * `unified` and `remark-parse` are react-markdown's own dependencies, so this
 * adds no package and — since DocsMarkdown already ships the parser — no
 * bytes to the client bundle.
 */

import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import type { Nodes, Root } from 'mdast';
import { slugifyHeading, type DocHeading } from '@/content/docs/headings';

const parser = unified().use(remarkParse).use(remarkGfm);

/**
 * A node's text AS RENDERED, which is not quite mdast-util-to-string:
 *  - an image renders an <img>, whose alt is not text content, so it adds
 *    nothing (the DOM side, DocsMarkdown's extractText, sees no children);
 *  - raw `html` is NOT dropped: without rehype-raw, react-markdown shows it
 *    as literal text (`## Before <br> after` renders the characters `<br>`,
 *    measured 2026-09-13), so its source is the text;
 *  - a hard break renders a <br>, which has no text content;
 *  - a GFM footnote reference renders as its number link, which no heading
 *    here uses; it is treated as empty rather than guessed at.
 */
function renderedText(node: Nodes): string {
  switch (node.type) {
    case 'image':
    case 'imageReference':
    case 'break':
    case 'footnoteReference':
    case 'definition':
    case 'footnoteDefinition':
      return '';
    case 'text':
    case 'inlineCode':
    case 'html':
      return node.value;
    default:
      return 'children' in node
        ? (node.children as Nodes[]).map(renderedText).join('')
        : '';
  }
}

/** The `##` and `###` headings of one body, in document order. */
export function docHeadingsOf(body: string): DocHeading[] {
  const tree = parser.runSync(parser.parse(body)) as Root;
  const out: DocHeading[] = [];
  // Not top-level only: a `##` inside a blockquote or list item is still an
  // <h2> with an id once react-markdown renders it, so every container is
  // walked, not just the root's children.
  const visit = (node: Nodes) => {
    if (node.type === 'heading') {
      if (node.depth === 2 || node.depth === 3) {
        const text = renderedText(node).trim();
        out.push({ id: slugifyHeading(text), text, depth: node.depth });
      }
      return;
    }
    if ('children' in node) (node.children as Nodes[]).forEach(visit);
  };
  visit(tree);
  return out;
}
