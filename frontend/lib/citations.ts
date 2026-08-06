/**
 * Strip the model's inline [n] web-citation markers for display (owner
 * request 2026-08-05): "90 million streams [3][9]" → "90 million streams".
 * The numbered sources are not lost — they live in the ActivityPanel's
 * "Cited sources" list behind the action row's Sources button.
 *
 * Code is untouched: fenced blocks and inline code spans keep their brackets
 * (`arr[0]` must render exactly as written), and markdown link syntax
 * survives — `[1](url)` and `[1]: url` are links, not citations.
 */

/**
 * One citation run — `[3]`, `[3][9]`, `[12][3][9]` — plus the single space
 * before it, so "streams [3][9] and" collapses to "streams and":
 *  - `(?<![\w\]])` the opening bracket may not follow a word char (arr[0])
 *    or a `]` (that is the middle of a run the match already consumed);
 *  - `(?![:(])` a trailing `:` or `(` means link syntax, leave it alone.
 */
const CITATION_RUN = / ?(?<![\w\]])(?:\[\d{1,3}\])+(?![:(])/g;

function stripProse(segment: string): string {
  return (
    segment
      .replace(CITATION_RUN, '')
      // "streams [9]." leaves "streams ." — pull the punctuation back in.
      .replace(/ +([.,;:!?])/g, '$1')
  );
}

export function stripCitations(markdown: string): string {
  // Fenced code first; odd split indexes are the untouchable blocks.
  return markdown
    .split(/(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$))/)
    .map((part, i) => {
      if (i % 2 === 1) return part;
      // Inside a prose part, inline code spans are untouchable too.
      return part
        .split(/(`[^`\n]*`)/)
        .map((span, j) => (j % 2 === 1 ? span : stripProse(span)))
        .join('');
    })
    .join('');
}
