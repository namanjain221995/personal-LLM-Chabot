/**
 * Heading ids — computed from the markdown SOURCE here, and from the rendered
 * heading text in components/docs/DocsMarkdown.tsx, by this same function.
 *
 * That is the whole point of the shared helper. The sidebar's "On this page"
 * list is built from the source; the `id` attribute a browser scrolls to is
 * put on the element at render time. Two spellings of "slugify" would give a
 * table of contents whose links quietly go nowhere, which is the classic
 * documentation-site bug and is invisible to everything except a click.
 *
 * Inline markup is stripped BEFORE slugging (`## The \`error\` envelope` ->
 * `the-error-envelope`) because the renderer only ever sees the text: remark
 * has already turned the backticks into a <code> element by then, so a
 * slugifier that kept them would disagree with itself.
 */

export interface DocHeading {
  id: string;
  text: string;
  /** 2 for `##`, 3 for `###`. `#` is the page title, which is not in the body. */
  depth: 2 | 3;
}

export function slugifyHeading(text: string): string {
  return text
    .replace(/[`*_]/g, '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/**
 * A heading's source reduced to the text the renderer will show.
 *
 * 2026-09-13, verifier finding: the source side slugged the RAW line while
 * the renderer slugged the rendered text, so `## Limits attached to a
 * [model](/docs/rate-limits)` put `...-a-model-docs-rate-limits` in "On this
 * page" and `...-a-model` on the element — a contents link that scrolls
 * nowhere. Links and images keep only their text, as remark renders them;
 * the emphasis and code markers go as before. `slugifyHeading` then sees the
 * same string on both sides.
 */
function inlineText(source: string): string {
  return source
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[`*]/g, '')
    .trim();
}

/**
 * The `##` and `###` headings of one body, in document order.
 *
 * Fenced code is skipped. A Python comment (`# install the client`) or a
 * shell prompt inside a sample is not a heading, and a table of contents
 * that thought otherwise would list it and link to nothing.
 */
export function headingsOf(body: string): DocHeading[] {
  const out: DocHeading[] = [];
  let fence: string | null = null;
  for (const line of body.split('\n')) {
    const fenceMatch = /^(~~~+|```+)/.exec(line.trim());
    if (fenceMatch) {
      const marker = fenceMatch[1][0];
      if (fence === null) fence = marker;
      else if (fence === marker) fence = null;
      continue;
    }
    if (fence !== null) continue;
    const heading = /^(#{2,3})\s+(.*\S)\s*$/.exec(line);
    if (!heading) continue;
    const text = inlineText(heading[2]);
    out.push({
      id: slugifyHeading(text),
      text,
      depth: heading[1].length === 2 ? 2 : 3,
    });
  }
  return out;
}
